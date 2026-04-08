"""Container lifecycle orchestration.

Sits between the REST API layer, the SQLite database, and the Docker
client.  All container state transitions flow through this module.
"""

import logging
import os
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
from orchestrator.models import ContainerResponse, ContainerStatus, CreateContainerRequest

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
    def __init__(self, config: Config, db: Database, docker: DockerClient) -> None:
        self._config = config
        self._db = db
        self._docker = docker

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

        # 2. Generate ID and derive socket path
        container_id = generate_id()
        socket_path = os.path.join(self._config.socket_dir, f"{container_id}.sock")

        # 3. Build Docker container configuration
        env_list = [f"{k}={v}" for k, v in req.env.items()]
        env_list.append(f"DROVER_CONTAINER_ID={container_id}")

        # Socket bind mount is added in Phase 4 once socket_manager creates
        # the Unix socket file before container start.  Including the mount
        # now would cause Docker to create a *directory* at the socket path
        # (the file doesn't exist yet), which breaks later socket creation.
        binds: list[str] = []
        if req.privileged:
            binds.append(f"{self._config.docker_sock}:/run/docker.sock")

        host_config: dict = {"Binds": binds} if binds else {}
        if not req.privileged:
            host_config["Runtime"] = "runsc"

        docker_config = {
            "Image": image,
            "Env": env_list,
            "HostConfig": host_config,
        }

        # 4. Ensure socket directory exists (needed later by socket_manager)
        os.makedirs(self._config.socket_dir, exist_ok=True)

        # 5. Create and start Docker container
        result = await self._docker.create_container(docker_config)
        docker_id = result["Id"]

        try:
            await self._docker.start_container(docker_id)
        except Exception:
            # Clean up the Docker container if start fails
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

        # Stop first (if running), then remove
        docker_id = row["docker_id"]
        try:
            await self._docker.stop_container(docker_id)
        except (ContainerNotFoundError, ContainerConflictError):
            pass  # Already stopped or gone

        try:
            await self._docker.remove_container(docker_id, force=True)
        except ContainerNotFoundError:
            pass  # Already removed

        # Clean up socket file if present
        socket_path = row["socket_path"]
        if socket_path:
            try:
                os.unlink(socket_path)
            except FileNotFoundError:
                pass

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
