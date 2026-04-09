"""Container lifecycle orchestration.

Sits between the REST API layer, the SQLite database, and the Docker
client.  All container state transitions flow through this module.
"""

import logging
from datetime import datetime, timezone

from orchestrator.config import Config
from orchestrator.database import Database
from orchestrator.docker_client import (
    ContainerConflictError,
    ContainerNotFoundError,
    DockerClient,
    ImageNotFoundError,
)
from orchestrator.id_gen import generate_id
from orchestrator.models import (
    CommandMessage,
    ContainerResponse,
    ContainerStatus,
    CreateContainerRequest,
    ExecStatusResponse,
)
from orchestrator.socket_manager import SocketManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ContainerError(Exception):
    """Base exception for container operations."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class ContainerNotFound(ContainerError):
    def __init__(self, container_id: str) -> None:
        super().__init__(404, f"Container '{container_id}' not found")


class ContainerStateConflict(ContainerError):
    def __init__(self, container_id: str, current: str, action: str) -> None:
        super().__init__(
            409,
            f"Cannot {action} container '{container_id}' in state '{current}'",
        )


class PrivilegedNotConfigured(ContainerError):
    def __init__(self) -> None:
        super().__init__(
            400,
            "Privileged containers are not configured (PRIVILEGED_IMAGE is unset)",
        )


class ImageNotFound(ContainerError):
    def __init__(self, image: str) -> None:
        super().__init__(404, f"Image 'drover/{image}' not found")


class ContainerNotConnected(ContainerError):
    def __init__(self, container_id: str) -> None:
        super().__init__(
            409,
            f"Container '{container_id}' has no active guest agent connection",
        )


class CommandNotFound(ContainerError):
    def __init__(self, container_id: str, command_id: str) -> None:
        super().__init__(
            404,
            f"Command '{command_id}' not found on container '{container_id}'",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_response(row) -> ContainerResponse:
    return ContainerResponse(
        id=row["id"],
        image=row["image"],
        privileged=bool(row["privileged"]),
        status=ContainerStatus(row["status"]),
        label=row["label"],
        timeout_seconds=row["timeout_seconds"],
        created_at=row["created_at"],
        stopped_at=row["stopped_at"],
        last_seen=row["last_seen"],
    )


def _docker_state_to_status(inspection: dict) -> ContainerStatus | None:
    """Map Docker inspect state to our status enum.

    Returns None for Docker states that don't map cleanly (e.g. "created",
    "restarting") so the caller can skip the sync.
    """
    docker_status = inspection.get("State", {}).get("Status", "")
    if docker_status == "running":
        return ContainerStatus.running
    if docker_status in ("exited", "dead", "paused"):
        return ContainerStatus.stopped
    return None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ContainerManager:
    def __init__(
        self, config: Config, db: Database, docker: DockerClient, sockets: SocketManager
    ) -> None:
        self._config = config
        self._db = db
        self._docker = docker
        self._sockets = sockets

    # -- startup sync -------------------------------------------------------

    async def sync_containers(self) -> None:
        """Reconcile all non-terminal containers against Docker state.

        Called once at startup to handle the case where the orchestrator
        crashed or restarted while containers were running.  For each
        non-destroyed container we inspect Docker and:
          - mark it destroyed if the Docker container is gone,
          - update the DB status if Docker disagrees (and we're not
            mid-transition),
          - re-establish a socket listener for containers still running.
        """
        rows = await self._db.fetchall(
            "SELECT id, docker_id, status, socket_path FROM containers "
            "WHERE status != 'destroyed'",
        )
        if not rows:
            return

        logger.info("Startup sync: reconciling %d non-terminal containers", len(rows))

        for row in rows:
            container_id = row["id"]
            docker_id = row["docker_id"]
            db_status = row["status"]

            try:
                inspection = await self._docker.inspect_container(docker_id)
                mapped = _docker_state_to_status(inspection)

                if (
                    mapped
                    and mapped.value != db_status
                    and db_status not in ("stopping", "resuming", "destroying")
                ):
                    await self._db.execute_insert(
                        "UPDATE containers SET status = ? WHERE id = ?",
                        (mapped.value, container_id),
                    )
                    logger.info(
                        "Startup sync: container %s status %s -> %s",
                        container_id,
                        db_status,
                        mapped.value,
                    )
                    db_status = mapped.value

                # Re-establish socket listener for containers still running
                if db_status == "running":
                    await self._sockets.create_socket(container_id)
                    logger.info(
                        "Startup sync: re-established socket for container %s",
                        container_id,
                    )

            except ContainerNotFoundError:
                await self._db.execute_insert(
                    "UPDATE containers SET status = 'destroyed' WHERE id = ?",
                    (container_id,),
                )
                logger.warning(
                    "Startup sync: container %s not found in Docker, marked destroyed",
                    container_id,
                )
            except Exception:
                logger.exception(
                    "Startup sync: failed to reconcile container %s", container_id
                )

    # -- create -------------------------------------------------------------

    async def create_container(self, req: CreateContainerRequest) -> ContainerResponse:
        # 1. Validate image / privileged config
        if req.privileged:
            if not self._config.privileged_image:
                raise PrivilegedNotConfigured()
            image = self._config.privileged_image
        else:
            image = f"drover/{req.image}"
            try:
                await self._docker.inspect_image(image)
            except ImageNotFoundError:
                raise ImageNotFound(req.image)

        # 2. Generate ID and create the Unix socket (must exist before container
        #    start so the bind mount targets a file, not a directory).
        container_id = generate_id()
        socket_path = await self._sockets.create_socket(container_id)

        # 3. Build Docker container configuration
        env_list = [f"{k}={v}" for k, v in req.env.items()]
        env_list.append(f"DROVER_CONTAINER_ID={container_id}")

        binds: list[str] = [f"{socket_path}:/run/orchestrator.sock"]
        if req.privileged:
            binds.append(f"{self._config.docker_sock}:/run/docker.sock")

        host_config: dict = {"Binds": binds}
        if not req.privileged:
            host_config["Runtime"] = "runsc"

        docker_config = {
            "Image": image,
            "Env": env_list,
            "HostConfig": host_config,
        }

        # 4. Create and start Docker container
        result = await self._docker.create_container(docker_config)
        docker_id = result["Id"]

        try:
            await self._docker.start_container(docker_id)
        except Exception:
            # Clean up the Docker container and socket if start fails
            await self._sockets.destroy_socket(container_id)
            try:
                await self._docker.remove_container(docker_id, force=True)
            except Exception:
                pass
            raise

        # 6. Persist to SQLite
        now = _now_iso()
        await self._db.execute_insert(
            """INSERT INTO containers
               (id, docker_id, image, privileged, status, socket_path,
                label, timeout_seconds, last_seen, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                container_id,
                docker_id,
                req.image,
                int(req.privileged),
                "running",
                socket_path,
                req.label,
                req.timeout_seconds,
                now,
                now,
            ),
        )

        logger.info(
            "Created container %s (docker=%s, image=%s, privileged=%s)",
            container_id,
            docker_id[:12],
            req.image,
            req.privileged,
        )

        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        return _row_to_response(row)

    # -- get ----------------------------------------------------------------

    async def get_container(self, container_id: str) -> ContainerResponse:
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        if not row:
            raise ContainerNotFound(container_id)

        # Sync with Docker unless the container is already in a terminal state
        current_status = row["status"]
        if current_status != "destroyed":
            try:
                inspection = await self._docker.inspect_container(row["docker_id"])
                mapped = _docker_state_to_status(inspection)
                # Only update if Docker disagrees AND we're not mid-transition
                if (
                    mapped
                    and mapped.value != current_status
                    and current_status
                    not in ("stopping", "resuming", "destroying")
                ):
                    await self._db.execute_insert(
                        "UPDATE containers SET status = ? WHERE id = ?",
                        (mapped.value, container_id),
                    )
                    logger.info(
                        "Container %s status synced: %s -> %s",
                        container_id,
                        current_status,
                        mapped.value,
                    )
                    row = await self._db.fetchone(
                        "SELECT * FROM containers WHERE id = ?", (container_id,)
                    )
            except ContainerNotFoundError:
                # Docker container gone — mark as destroyed
                await self._db.execute_insert(
                    "UPDATE containers SET status = 'destroyed' WHERE id = ?",
                    (container_id,),
                )
                logger.warning(
                    "Container %s not found in Docker, marked destroyed",
                    container_id,
                )
                row = await self._db.fetchone(
                    "SELECT * FROM containers WHERE id = ?", (container_id,)
                )

        return _row_to_response(row)

    # -- stop ---------------------------------------------------------------

    async def stop_container(self, container_id: str) -> ContainerResponse:
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        if not row:
            raise ContainerNotFound(container_id)
        if row["status"] != "running":
            raise ContainerStateConflict(container_id, row["status"], "stop")

        await self._db.execute_insert(
            "UPDATE containers SET status = 'stopping' WHERE id = ?",
            (container_id,),
        )

        # Close the socket connection but preserve the socket file for resume
        await self._sockets.close_socket(container_id)

        try:
            await self._docker.stop_container(row["docker_id"])
        except ContainerNotFoundError:
            pass  # Already gone

        now = _now_iso()
        await self._db.execute_insert(
            "UPDATE containers SET status = 'stopped', stopped_at = ? WHERE id = ?",
            (now, container_id),
        )

        logger.info("Stopped container %s", container_id)
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        return _row_to_response(row)

    # -- resume -------------------------------------------------------------

    async def resume_container(self, container_id: str) -> ContainerResponse:
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        if not row:
            raise ContainerNotFound(container_id)
        if row["status"] != "stopped":
            raise ContainerStateConflict(container_id, row["status"], "resume")

        await self._db.execute_insert(
            "UPDATE containers SET status = 'resuming' WHERE id = ?",
            (container_id,),
        )

        # Re-create the socket listener before starting the container
        await self._sockets.create_socket(container_id)

        await self._docker.start_container(row["docker_id"])

        now = _now_iso()
        await self._db.execute_insert(
            "UPDATE containers SET status = 'running', stopped_at = NULL, last_seen = ? WHERE id = ?",
            (now, container_id),
        )

        logger.info("Resumed container %s", container_id)
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        return _row_to_response(row)

    # -- destroy ------------------------------------------------------------

    async def destroy_container(self, container_id: str) -> ContainerResponse:
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        if not row:
            raise ContainerNotFound(container_id)
        if row["status"] == "destroyed":
            raise ContainerStateConflict(container_id, row["status"], "destroy")

        await self._db.execute_insert(
            "UPDATE containers SET status = 'destroying' WHERE id = ?",
            (container_id,),
        )

        # Close and remove the socket, then stop and remove the Docker container
        await self._sockets.destroy_socket(container_id)

        docker_id = row["docker_id"]
        try:
            await self._docker.stop_container(docker_id)
        except (ContainerNotFoundError, ContainerConflictError):
            pass  # Already stopped or gone

        try:
            await self._docker.remove_container(docker_id, force=True)
        except ContainerNotFoundError:
            pass  # Already removed

        # Preserve existing stopped_at if already set (container was stopped
        # before being destroyed), otherwise record the current time.
        stopped_at = row["stopped_at"] or _now_iso()
        await self._db.execute_insert(
            "UPDATE containers SET status = 'destroyed', stopped_at = ? WHERE id = ?",
            (stopped_at, container_id),
        )

        logger.info("Destroyed container %s", container_id)
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        return _row_to_response(row)

    # -- exec ---------------------------------------------------------------

    async def exec_command(self, container_id: str, command: str) -> str:
        """Send a command to a running container.  Returns the command ID."""
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        if not row:
            raise ContainerNotFound(container_id)
        if row["status"] != "running":
            raise ContainerStateConflict(container_id, row["status"], "exec on")

        if not self._sockets.is_connected(container_id):
            raise ContainerNotConnected(container_id)

        command_id = generate_id()
        now = _now_iso()

        await self._db.execute_insert(
            "INSERT INTO commands (id, container_id, command, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (command_id, container_id, command, now),
        )

        await self._sockets.send_command(container_id, command_id, command)

        logger.info(
            "Exec command %s on container %s: %s",
            command_id,
            container_id,
            command[:80],
        )
        return command_id

    async def get_command_status(
        self, container_id: str, command_id: str
    ) -> ExecStatusResponse:
        """Get the status and output of a command."""
        # Verify container exists
        container = await self._db.fetchone(
            "SELECT id FROM containers WHERE id = ?", (container_id,)
        )
        if not container:
            raise ContainerNotFound(container_id)

        # Fetch command
        cmd_row = await self._db.fetchone(
            "SELECT * FROM commands WHERE id = ? AND container_id = ?",
            (command_id, container_id),
        )
        if not cmd_row:
            raise CommandNotFound(container_id, command_id)

        # Fetch messages ordered by seq
        msg_rows = await self._db.fetchall(
            "SELECT seq, stream, data FROM command_messages "
            "WHERE command_id = ? ORDER BY seq",
            (command_id,),
        )

        return ExecStatusResponse(
            command_id=command_id,
            status=cmd_row["status"],
            exit_code=cmd_row["exit_code"],
            messages=[
                CommandMessage(seq=r["seq"], stream=r["stream"], data=r["data"])
                for r in msg_rows
            ],
        )
