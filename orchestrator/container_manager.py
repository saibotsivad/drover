"""Container lifecycle orchestration.

Sits between the REST API layer, the SQLite database, and the Docker
client.  All container state transitions flow through this module.

Both initialization and resume are asynchronous: ``create_container`` and
``resume_container`` insert / update the DB row (status ``initializing`` or
``resuming`` respectively) and return immediately.  A background task
performs the Docker create/start work, and the container transitions to
``running`` only once the guest agent sends a ``ready`` message.  A
watchdog task fails the container to ``error`` if the transition does not
complete within ``init_timeout_seconds``.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from orchestrator.config import Config
from orchestrator.database import Database
from orchestrator.docker_client import DockerClient
from orchestrator.errors import (
    CommandNotFound,
    ContainerNotConnected,
    ContainerNotFound,
    ContainerNotFoundError,
    ContainerStateConflict,
    ImageNotFound,
    PrivilegedNotConfigured,
    ContainerConflictError,
)
from orchestrator.id_gen import generate_id
from orchestrator.log_capture import LogCaptureManager
from orchestrator.models import (
    CommandMessage,
    CommandStatus,
    CommandSummary,
    ContainerResponse,
    ContainerStatus,
    CreateContainerRequest,
    ExecStatusResponse,
)
from orchestrator.socket_manager import SocketManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Matches the default timeout passed to Docker's stop API (SIGTERM wait before SIGKILL).
_DOCKER_STOP_TIMEOUT = 10


def _row_to_response(
    row, transition_timeout_seconds: int | None = None
) -> ContainerResponse:
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
        error_code=row["error_code"],
        transition_timeout_seconds=transition_timeout_seconds,
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
        self,
        config: Config,
        db: Database,
        docker: DockerClient,
        sockets: SocketManager,
        log_capture: LogCaptureManager,
        host_docker_sock: str,
        host_socket_dir: str,
    ) -> None:
        self._config = config
        self._db = db
        self._docker = docker
        self._sockets = sockets
        self._logs = log_capture
        # Host-side paths resolved during lifespan startup via self-inspect
        # (see app.py).  Docker resolves bind-mount sources against the host
        # filesystem, so the orchestrator's in-container views of these paths
        # are wrong when the host and container paths differ (e.g. rootless
        # Docker with a relative sockets directory).
        self._host_docker_sock = host_docker_sock
        self._host_socket_dir = host_socket_dir
        # In-flight background initialization tasks keyed by container id.
        # Used for cancellation on ready-receipt and on orchestrator shutdown.
        self._init_tasks: dict[str, asyncio.Task] = {}
        self._init_watchdogs: dict[str, asyncio.Task] = {}

    # -- shutdown -----------------------------------------------------------

    async def shutdown(self) -> None:
        """Cancel any in-flight init and watchdog tasks on app shutdown."""
        tasks = list(self._init_watchdogs.values()) + list(self._init_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._init_tasks.clear()
        self._init_watchdogs.clear()

    async def on_container_ready(self, container_id: str) -> None:
        """Cancel the init/resume watchdog after a ``ready`` message is received.

        Invoked by ``SocketManager`` via the ready callback.  The DB status
        has already been transitioned from ``initializing`` or ``resuming``
        to ``running`` by the socket manager before this is called.
        """
        watchdog = self._init_watchdogs.pop(container_id, None)
        if watchdog and not watchdog.done():
            watchdog.cancel()

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

        Rows in ``initializing`` or ``resuming`` are skipped during
        reconciliation because the background task that owned them was
        interrupted by the restart and the Docker container may or may not
        be in the expected state.  After the reconciliation pass, any
        remaining ``initializing`` / ``resuming`` rows are transitioned to
        ``error`` with code ``orchestrator_crash``.
        """
        rows = await self._db.fetchall(
            "SELECT id, docker_id, status, socket_path FROM containers "
            "WHERE status NOT IN ('destroyed', 'error')",
        )
        if not rows:
            return

        logger.info("Startup sync: reconciling %d non-terminal containers", len(rows))

        for row in rows:
            container_id = row["id"]
            docker_id = row["docker_id"]
            db_status = row["status"]

            if db_status in ("initializing", "resuming"):
                # Crashed mid-init / mid-resume; handled by the
                # post-reconciliation pass below.
                continue

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

                # Re-establish socket listener and log capture for
                # containers still running.
                if db_status == "running":
                    await self._sockets.create_socket(container_id)
                    cursor = self._logs.read_cursor(container_id)
                    try:
                        await self._logs.start(
                            container_id, docker_id, since=cursor
                        )
                    except Exception:
                        logger.exception(
                            "Startup sync: log capture failed to resume for %s",
                            container_id,
                        )
                    logger.info(
                        "Startup sync: re-established socket for container %s",
                        container_id,
                    )

            except ContainerNotFoundError:
                await self._db.execute_insert(
                    "UPDATE containers SET status = 'destroyed' WHERE id = ?",
                    (container_id,),
                )
                # Belt-and-braces: ensure the on-disk capture directory is
                # cleaned up for rows the reconciler just marked destroyed.
                try:
                    await self._logs.discard(container_id)
                except Exception:
                    logger.exception(
                        "Startup sync: failed to discard log capture for %s",
                        container_id,
                    )
                logger.warning(
                    "Startup sync: container %s not found in Docker, marked destroyed",
                    container_id,
                )
            except Exception:
                logger.exception(
                    "Startup sync: failed to reconcile container %s", container_id
                )

        # Any container still in ``initializing`` or ``resuming`` was
        # interrupted by the restart.  Force-remove the Docker container if
        # one was created and transition to ``error`` with code
        # ``orchestrator_crash``.
        stuck_rows = await self._db.fetchall(
            "SELECT id, docker_id, status FROM containers "
            "WHERE status IN ('initializing', 'resuming')",
        )
        for row in stuck_rows:
            container_id = row["id"]
            docker_id = row["docker_id"]
            source_status = row["status"]
            await self._fail_init(
                container_id, "orchestrator_crash", docker_id, source_status
            )
            logger.warning(
                "Startup sync: container %s was stuck in %s, marked error",
                container_id,
                source_status,
            )

    # -- create -------------------------------------------------------------

    async def create_container(self, req: CreateContainerRequest) -> ContainerResponse:
        """Create a container row and schedule background Docker initialization.

        Returns immediately with status ``initializing``.  The background
        task creates the socket, creates and starts the Docker container,
        and the container transitions to ``running`` only when the guest
        agent sends a ``ready`` message.  A watchdog task transitions the
        container to ``error`` if initialization exceeds the configured
        timeout.
        """
        # 1. Validate image / privileged config
        if req.privileged:
            if not self._config.privileged_image:
                raise PrivilegedNotConfigured()
            image = self._config.privileged_image
        else:
            # Resolve the short name supplied by the caller to a concrete
            # Docker image via the ``drover.name`` label.  Prefer the image
            # ID so the create call is unambiguous even if the same image
            # carries multiple tags.
            matches = await self._docker.list_images(name=req.image)
            if not matches:
                raise ImageNotFound(req.image)
            image = matches[0]["Id"]

        # 2. Generate ID and insert DB row in initializing state
        container_id = generate_id()
        now = _now_iso()
        await self._db.execute_insert(
            """INSERT INTO containers
               (id, docker_id, image, privileged, status, socket_path,
                label, timeout_seconds, last_seen, created_at, error_code)
               VALUES (?, NULL, ?, ?, 'initializing', NULL, ?, ?, ?, ?, NULL)""",
            (
                container_id,
                req.image,
                int(req.privileged),
                req.label,
                req.timeout_seconds,
                now,
                now,
            ),
        )

        # 3. Schedule the background Docker init and the timeout watchdog.
        init_task = asyncio.create_task(
            self._init_container(container_id, req, image)
        )
        self._init_tasks[container_id] = init_task
        init_task.add_done_callback(
            lambda _t, cid=container_id: self._init_tasks.pop(cid, None)
        )

        watchdog_task = asyncio.create_task(
            self._init_watchdog(container_id, init_task, "initializing", "init_timeout")
        )
        self._init_watchdogs[container_id] = watchdog_task
        watchdog_task.add_done_callback(
            lambda _t, cid=container_id: self._init_watchdogs.pop(cid, None)
        )

        logger.info(
            "Created container %s (image=%s, privileged=%s); init scheduled",
            container_id,
            req.image,
            req.privileged,
        )

        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        return _row_to_response(row, self._config.init_timeout_seconds)

    # -- background init ---------------------------------------------------

    async def _init_container(
        self, container_id: str, req: CreateContainerRequest, image: str
    ) -> None:
        """Phase-2 initialization: socket, Docker create, Docker start.

        Runs as a background task.  On Docker failure, transitions the
        container to ``error`` with code ``init_docker_error`` and cleans
        up.  On success, the container remains ``initializing`` until the
        guest agent sends ``ready``.
        """
        docker_id: str | None = None
        try:
            # Socket must exist before the Docker container starts so the
            # bind-mount target is a file rather than a directory.
            socket_path = await self._sockets.create_socket(container_id)
            await self._db.execute_insert(
                "UPDATE containers SET socket_path = ? WHERE id = ?",
                (socket_path, container_id),
            )

            env_list = [f"{k}={v}" for k, v in req.env.items()]
            env_list.append(f"DROVER_CONTAINER_ID={container_id}")

            # Use the HOST-side socket path as the bind source — Docker
            # resolves bind sources against the host filesystem, not the
            # orchestrator container's filesystem.
            host_socket_path = os.path.join(
                self._host_socket_dir, f"{container_id}.sock"
            )
            logger.info(
                "Container %s: socket in-container=%s host=%s",
                container_id, socket_path, host_socket_path,
            )
            binds: list[str] = [f"{host_socket_path}:/run/orchestrator.sock"]
            if req.privileged:
                binds.append(f"{self._host_docker_sock}:/run/docker.sock")

            host_config: dict = {"Binds": binds}
            if not req.privileged:
                host_config["Runtime"] = "runsc"

            docker_config = {
                "Image": image,
                "Env": env_list,
                "HostConfig": host_config,
            }

            result = await self._docker.create_container(docker_config)
            docker_id = result["Id"]
            await self._db.execute_insert(
                "UPDATE containers SET docker_id = ? WHERE id = ?",
                (docker_id, container_id),
            )

            await self._docker.start_container(docker_id)

            # Start log capture immediately so init-window stdout/stderr is
            # retained even when the guest agent never connects.  Failures
            # to start capture are non-fatal for the container — log and
            # continue.
            try:
                await self._logs.start(container_id, docker_id, since=None)
            except Exception:
                logger.exception(
                    "Container %s: log capture failed to start", container_id
                )

            logger.info(
                "Container %s Docker init complete (docker=%s); awaiting ready",
                container_id,
                docker_id[:12],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Container %s failed during Docker initialization", container_id
            )
            await self._fail_init(container_id, "init_docker_error", docker_id)

    async def _init_watchdog(
        self,
        container_id: str,
        init_task: asyncio.Task,
        source_status: str,
        timeout_error_code: str,
    ) -> None:
        """Fail a container to ``error`` if init/resume does not complete in time.

        Sleeps for ``init_timeout_seconds`` and then checks whether the
        container is still in ``source_status`` (``initializing`` for first
        init, ``resuming`` for resume).  If so, cancels the background task,
        marks the row as ``error`` with code ``timeout_error_code``, and
        cleans up.  The watchdog is cancelled when ``ready`` is received or
        when the background task itself fails and transitions to ``error``
        first.
        """
        try:
            await asyncio.sleep(self._config.init_timeout_seconds)
        except asyncio.CancelledError:
            return

        row = await self._db.fetchone(
            "SELECT status, docker_id FROM containers WHERE id = ?",
            (container_id,),
        )
        if not row or row["status"] != source_status:
            return

        if not init_task.done():
            init_task.cancel()
            try:
                await init_task
            except (asyncio.CancelledError, Exception):
                pass

        logger.warning(
            "Container %s %s timed out after %ds",
            container_id,
            source_status,
            self._config.init_timeout_seconds,
        )
        await self._fail_init(
            container_id, timeout_error_code, row["docker_id"], source_status
        )

    async def _fail_init(
        self,
        container_id: str,
        error_code: str,
        docker_id: str | None,
        source_status: str = "initializing",
    ) -> None:
        """Transition an initializing/resuming container to ``error`` and clean up.

        The UPDATE is conditional on the row still being in
        ``source_status`` so a late ``ready`` or a racing watchdog fire
        cannot overwrite a successful transition.  Cleanup (socket +
        Docker) only runs if this call actually performed the transition.
        """
        async with self._db.execute(
            "UPDATE containers SET status = 'error', error_code = ? "
            "WHERE id = ? AND status = ?",
            (error_code, container_id, source_status),
        ) as cursor:
            rowcount = cursor.rowcount

        if rowcount == 0:
            return

        # Stop the log writer before tearing the Docker container down so
        # the final captured chunks are persisted (directory is preserved
        # for post-mortem of the failed init).
        try:
            await self._logs.stop(container_id)
        except Exception:
            logger.exception(
                "Failed to stop log capture for container %s", container_id
            )

        try:
            await self._sockets.destroy_socket(container_id)
        except Exception:
            logger.exception(
                "Failed to destroy socket for container %s", container_id
            )

        if docker_id:
            try:
                await self._docker.remove_container(docker_id, force=True)
            except Exception:
                logger.exception(
                    "Failed to force-remove Docker container %s for %s",
                    docker_id,
                    container_id,
                )

    # -- list ---------------------------------------------------------------

    async def list_containers(self) -> list[ContainerResponse]:
        """Return all containers, newest first.

        Reads straight from the DB without per-row Docker reconciliation;
        ``get_container`` syncs an individual row, and the reaper handles
        background drift.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM containers ORDER BY created_at DESC"
        )
        return [_row_to_response(row) for row in rows]

    # -- get ----------------------------------------------------------------

    async def get_container(self, container_id: str) -> ContainerResponse:
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        if not row:
            raise ContainerNotFound(container_id)

        # Sync with Docker unless the container is in a terminal state or
        # still initializing (the background task owns state transitions for
        # the latter, and docker_id may still be NULL).
        current_status = row["status"]
        if current_status not in ("destroyed", "error", "initializing"):
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

        # The follow stream is closing in the background; await the writer
        # task here so .cursor is persisted before stopped is observable.
        try:
            await self._logs.stop(container_id)
        except Exception:
            logger.exception(
                "Failed to stop log capture for container %s", container_id
            )

        now = _now_iso()
        await self._db.execute_insert(
            "UPDATE containers SET status = 'stopped', stopped_at = ? WHERE id = ?",
            (now, container_id),
        )

        logger.info("Stopped container %s", container_id)
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        return _row_to_response(row, _DOCKER_STOP_TIMEOUT)

    # -- resume -------------------------------------------------------------

    async def resume_container(self, container_id: str) -> ContainerResponse:
        """Begin resuming a stopped container.

        Returns immediately with status ``resuming``.  The Docker start and
        the wait for the guest agent's ``ready`` message run in the
        background, mirroring the ``initializing`` flow.  The container
        transitions to ``running`` only when ``ready`` arrives.  A
        watchdog transitions the row to ``error`` (code ``resume_timeout``)
        if ``ready`` does not arrive within ``init_timeout_seconds``.
        """
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

        resume_task = asyncio.create_task(
            self._resume_container_async(container_id, row["docker_id"])
        )
        self._init_tasks[container_id] = resume_task
        resume_task.add_done_callback(
            lambda _t, cid=container_id: self._init_tasks.pop(cid, None)
        )

        watchdog_task = asyncio.create_task(
            self._init_watchdog(container_id, resume_task, "resuming", "resume_timeout")
        )
        self._init_watchdogs[container_id] = watchdog_task
        watchdog_task.add_done_callback(
            lambda _t, cid=container_id: self._init_watchdogs.pop(cid, None)
        )

        logger.info("Resuming container %s; awaiting ready", container_id)
        row = await self._db.fetchone(
            "SELECT * FROM containers WHERE id = ?", (container_id,)
        )
        return _row_to_response(row, self._config.init_timeout_seconds)

    async def _resume_container_async(
        self, container_id: str, docker_id: str
    ) -> None:
        """Background work for resume: re-create socket, docker start, log capture.

        Mirrors ``_init_container`` but for an existing Docker container.
        On Docker failure, transitions the row to ``error`` with code
        ``resume_docker_error``.  On success, the row stays ``resuming``
        until the guest agent sends ``ready`` (handled by SocketManager).
        """
        try:
            # Re-create the socket listener before starting the container so
            # the agent has something to connect to when it boots back up.
            await self._sockets.create_socket(container_id)

            # Read the cursor before starting so log capture resumes from
            # the last captured timestamp.  None when no logs have been
            # captured yet (e.g. capture disabled on first run).
            cursor = self._logs.read_cursor(container_id)

            await self._docker.start_container(docker_id)

            try:
                await self._logs.start(container_id, docker_id, since=cursor)
            except Exception:
                logger.exception(
                    "Container %s: log capture failed to resume", container_id
                )

            logger.info(
                "Container %s Docker resume complete (docker=%s); awaiting ready",
                container_id,
                docker_id[:12],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Container %s failed during Docker resume", container_id
            )
            await self._fail_init(
                container_id, "resume_docker_error", docker_id, "resuming"
            )

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
        # docker_id may be NULL for containers that failed init before the
        # Docker create call completed (status 'error').
        if docker_id:
            try:
                await self._docker.stop_container(docker_id)
            except (ContainerNotFoundError, ContainerConflictError):
                pass  # Already stopped or gone

            try:
                await self._docker.remove_container(docker_id, force=True)
            except ContainerNotFoundError:
                pass  # Already removed

        # Drop the captured logs along with the rest of the container's
        # state — applies to errored and initializing rows too.
        try:
            await self._logs.discard(container_id)
        except Exception:
            logger.exception(
                "Failed to discard log capture for container %s", container_id
            )

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
        return _row_to_response(row, _DOCKER_STOP_TIMEOUT)

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

    async def list_commands(self, container_id: str) -> list[CommandSummary]:
        """Return all commands for a container, newest first."""
        container = await self._db.fetchone(
            "SELECT id FROM containers WHERE id = ?", (container_id,)
        )
        if not container:
            raise ContainerNotFound(container_id)

        rows = await self._db.fetchall(
            "SELECT id, command, status, exit_code, created_at FROM commands "
            "WHERE container_id = ? ORDER BY created_at DESC",
            (container_id,),
        )
        return [
            CommandSummary(
                command_id=row["id"],
                command=row["command"],
                status=CommandStatus(row["status"]),
                exit_code=row["exit_code"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

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
            command=cmd_row["command"],
            status=cmd_row["status"],
            exit_code=cmd_row["exit_code"],
            messages=[
                CommandMessage(seq=r["seq"], stream=r["stream"], data=r["data"])
                for r in msg_rows
            ],
        )
