"""Worker lifecycle orchestration.

Sits between the REST API layer, the SQLite database, and the Docker
client.  All worker state transitions flow through this module.

Both initialization and resume are asynchronous: ``create_worker`` and
``resume_worker`` insert / update the DB row (status ``initializing`` or
``resuming`` respectively) and return immediately.  A background task
performs the Docker create/start work, and the worker transitions to
``running`` only once the worker agent sends a ``ready`` message.  A
watchdog task fails the worker to ``error`` if the transition does not
complete within ``init_timeout_seconds``.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from orchestrator.config import SOCKET_DIR, Config
from orchestrator.database import Database
from orchestrator.docker_client import DockerClient
from orchestrator.errors import (
    CapabilityNotSupported,
    CommandNotFound,
    WorkerNotConnected,
    WorkerNotFound,
    WorkerNotFoundError,
    WorkerStateConflict,
    ImageNotFound,
    PrivilegedNotConfigured,
    WorkerConflictError,
)
from orchestrator.id_gen import generate_id
from orchestrator.log_capture import LogCaptureManager
from orchestrator.models import (
    CAPABILITIES_LABEL,
    CAPABILITY_EXEC,
    CommandMessage,
    CommandStatus,
    CommandSummary,
    WorkerResponse,
    WorkerStatus,
    CreateWorkerRequest,
    ExecStatusResponse,
    parse_capabilities,
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
) -> WorkerResponse:
    return WorkerResponse(
        id=row["id"],
        image=row["image"],
        privileged=bool(row["privileged"]),
        status=WorkerStatus(row["status"]),
        label=row["label"],
        timeout_seconds=row["timeout_seconds"],
        created_at=row["created_at"],
        stopped_at=row["stopped_at"],
        last_seen=row["last_seen"],
        error_code=row["error_code"],
        transition_timeout_seconds=transition_timeout_seconds,
    )


def _docker_state_to_status(inspection: dict) -> WorkerStatus | None:
    """Map Docker inspect state to our status enum.

    Returns None for Docker states that don't map cleanly (e.g. "created",
    "restarting") so the caller can skip the sync.
    """
    docker_status = inspection.get("State", {}).get("Status", "")
    if docker_status == "running":
        return WorkerStatus.running
    if docker_status in ("exited", "dead", "paused"):
        return WorkerStatus.stopped
    return None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class WorkerManager:
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
        # In-flight background initialization tasks keyed by worker id.
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

    async def on_worker_ready(self, worker_id: str) -> None:
        """Cancel the init/resume watchdog after a ``ready`` message is received.

        Invoked by ``SocketManager`` via the ready callback.  The DB status
        has already been transitioned from ``initializing`` or ``resuming``
        to ``running`` by the socket manager before this is called.
        """
        watchdog = self._init_watchdogs.pop(worker_id, None)
        if watchdog and not watchdog.done():
            watchdog.cancel()

    # -- startup sync -------------------------------------------------------

    async def sync_workers(self) -> None:
        """Reconcile all non-terminal workers against Docker state.

        Called once at startup to handle the case where the orchestrator
        crashed or restarted while workers were running.  For each
        non-destroyed worker we inspect Docker and:
          - mark it destroyed if the Docker container is gone,
          - update the DB status if Docker disagrees (and we're not
            mid-transition),
          - re-establish a socket listener for workers still running.

        Rows in ``initializing`` or ``resuming`` are skipped during
        reconciliation because the background task that owned them was
        interrupted by the restart and the Docker container may or may not
        be in the expected state.  After the reconciliation pass, any
        remaining ``initializing`` / ``resuming`` rows are transitioned to
        ``error`` with code ``orchestrator_crash``.
        """
        rows = await self._db.fetchall(
            "SELECT id, docker_id, status, socket_path FROM workers "
            "WHERE status NOT IN ('destroyed', 'error')",
        )
        if not rows:
            return

        logger.info("Startup sync: reconciling %d non-terminal workers", len(rows))

        for row in rows:
            worker_id = row["id"]
            docker_id = row["docker_id"]
            db_status = row["status"]

            if db_status in ("initializing", "resuming"):
                # Crashed mid-init / mid-resume; handled by the
                # post-reconciliation pass below.
                continue

            try:
                inspection = await self._docker.inspect_worker(docker_id)
                mapped = _docker_state_to_status(inspection)

                if (
                    mapped
                    and mapped.value != db_status
                    and db_status not in ("stopping", "resuming", "destroying")
                ):
                    await self._db.execute_insert(
                        "UPDATE workers SET status = ? WHERE id = ?",
                        (mapped.value, worker_id),
                    )
                    logger.info(
                        "Startup sync: worker %s status %s -> %s",
                        worker_id,
                        db_status,
                        mapped.value,
                    )
                    db_status = mapped.value

                # Re-establish socket listener and log capture for
                # workers still running.
                if db_status == "running":
                    await self._sockets.create_socket(worker_id)
                    cursor = self._logs.read_cursor(worker_id)
                    try:
                        await self._logs.start(
                            worker_id, docker_id, since=cursor
                        )
                    except Exception:
                        logger.exception(
                            "Startup sync: log capture failed to resume for %s",
                            worker_id,
                        )
                    logger.info(
                        "Startup sync: re-established socket for worker %s",
                        worker_id,
                    )

            except WorkerNotFoundError:
                await self._db.execute_insert(
                    "UPDATE workers SET status = 'destroyed' WHERE id = ?",
                    (worker_id,),
                )
                # Belt-and-braces: ensure the on-disk capture directory is
                # cleaned up for rows the reconciler just marked destroyed.
                try:
                    await self._logs.discard(worker_id)
                except Exception:
                    logger.exception(
                        "Startup sync: failed to discard log capture for %s",
                        worker_id,
                    )
                logger.warning(
                    "Startup sync: worker %s not found in Docker, marked destroyed",
                    worker_id,
                )
            except Exception:
                logger.exception(
                    "Startup sync: failed to reconcile worker %s", worker_id
                )

        # Any worker still in ``initializing`` or ``resuming`` was
        # interrupted by the restart.  Force-remove the Docker container if
        # one was created and transition to ``error`` with code
        # ``orchestrator_crash``.
        stuck_rows = await self._db.fetchall(
            "SELECT id, docker_id, status FROM workers "
            "WHERE status IN ('initializing', 'resuming')",
        )
        for row in stuck_rows:
            worker_id = row["id"]
            docker_id = row["docker_id"]
            source_status = row["status"]
            await self._fail_init(
                worker_id, "orchestrator_crash", docker_id, source_status
            )
            logger.warning(
                "Startup sync: worker %s was stuck in %s, marked error",
                worker_id,
                source_status,
            )

    # -- create -------------------------------------------------------------

    async def create_worker(self, req: CreateWorkerRequest) -> WorkerResponse:
        """Create a worker row and schedule background Docker initialization.

        Returns immediately with status ``initializing``.  The background
        task creates the socket, creates and starts the Docker container,
        and the worker transitions to ``running`` only when the worker
        agent sends a ``ready`` message.  A watchdog task transitions the
        worker to ``error`` if initialization exceeds the configured
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
        worker_id = generate_id()
        now = _now_iso()
        await self._db.execute_insert(
            """INSERT INTO workers
               (id, docker_id, image, privileged, status, socket_path,
                label, timeout_seconds, last_seen, created_at, error_code)
               VALUES (?, NULL, ?, ?, 'initializing', NULL, ?, ?, ?, ?, NULL)""",
            (
                worker_id,
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
            self._init_worker(worker_id, req, image)
        )
        self._init_tasks[worker_id] = init_task
        init_task.add_done_callback(
            lambda _t, wid=worker_id: self._init_tasks.pop(wid, None)
        )

        watchdog_task = asyncio.create_task(
            self._init_watchdog(worker_id, init_task, "initializing", "init_timeout")
        )
        self._init_watchdogs[worker_id] = watchdog_task
        watchdog_task.add_done_callback(
            lambda _t, wid=worker_id: self._init_watchdogs.pop(wid, None)
        )

        logger.info(
            "Created worker %s (image=%s, privileged=%s); init scheduled",
            worker_id,
            req.image,
            req.privileged,
        )

        row = await self._db.fetchone(
            "SELECT * FROM workers WHERE id = ?", (worker_id,)
        )
        return _row_to_response(row, self._config.init_timeout_seconds)

    # -- background init ---------------------------------------------------

    async def _init_worker(
        self, worker_id: str, req: CreateWorkerRequest, image: str
    ) -> None:
        """Phase-2 initialization: socket, Docker create, Docker start.

        Runs as a background task.  On Docker failure, transitions the
        worker to ``error`` with code ``init_docker_error`` and cleans
        up.  On success, the worker remains ``initializing`` until the
        worker agent sends ``ready``.
        """
        docker_id: str | None = None
        try:
            # Socket must exist before the Docker container starts so the
            # bind-mount target is a file rather than a directory.
            socket_path = await self._sockets.create_socket(worker_id)
            await self._db.execute_insert(
                "UPDATE workers SET socket_path = ? WHERE id = ?",
                (socket_path, worker_id),
            )

            env_list = [f"{k}={v}" for k, v in req.env.items()]
            env_list.append(f"DROVER_WORKER_ID={worker_id}")

            # Bind the per-worker socket FOLDER into the worker
            # at SOCKET_DIR, so the worker agent finds the control
            # socket at {SOCKET_DIR}/orchestrator.sock.  Use the
            # HOST-side folder path as the bind source — Docker resolves
            # bind sources against the host filesystem, not the
            # orchestrator container's filesystem.
            host_socket_dir = os.path.join(self._host_socket_dir, worker_id)
            logger.info(
                "Worker %s: socket in-container=%s host-dir=%s",
                worker_id, socket_path, host_socket_dir,
            )
            binds: list[str] = [f"{host_socket_dir}:{SOCKET_DIR}"]
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

            result = await self._docker.create_worker(docker_config)
            docker_id = result["Id"]
            await self._db.execute_insert(
                "UPDATE workers SET docker_id = ? WHERE id = ?",
                (docker_id, worker_id),
            )

            await self._docker.start_worker(docker_id)

            # Start log capture immediately so init-window stdout/stderr is
            # retained even when the worker agent never connects.  Failures
            # to start capture are non-fatal for the worker — log and
            # continue.
            try:
                await self._logs.start(worker_id, docker_id, since=None)
            except Exception:
                logger.exception(
                    "Worker %s: log capture failed to start", worker_id
                )

            logger.info(
                "Worker %s Docker init complete (docker=%s); awaiting ready",
                worker_id,
                docker_id[:12],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Worker %s failed during Docker initialization", worker_id
            )
            await self._fail_init(worker_id, "init_docker_error", docker_id)

    async def _init_watchdog(
        self,
        worker_id: str,
        init_task: asyncio.Task,
        source_status: str,
        timeout_error_code: str,
    ) -> None:
        """Fail a worker to ``error`` if init/resume does not complete in time.

        Sleeps for ``init_timeout_seconds`` and then checks whether the
        worker is still in ``source_status`` (``initializing`` for first
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
            "SELECT status, docker_id FROM workers WHERE id = ?",
            (worker_id,),
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
            "Worker %s %s timed out after %ds",
            worker_id,
            source_status,
            self._config.init_timeout_seconds,
        )
        await self._fail_init(
            worker_id, timeout_error_code, row["docker_id"], source_status
        )

    async def _fail_init(
        self,
        worker_id: str,
        error_code: str,
        docker_id: str | None,
        source_status: str = "initializing",
    ) -> None:
        """Transition an initializing/resuming worker to ``error`` and clean up.

        The UPDATE is conditional on the row still being in
        ``source_status`` so a late ``ready`` or a racing watchdog fire
        cannot overwrite a successful transition.  Cleanup (socket +
        Docker) only runs if this call actually performed the transition.
        """
        async with self._db.execute(
            "UPDATE workers SET status = 'error', error_code = ? "
            "WHERE id = ? AND status = ?",
            (error_code, worker_id, source_status),
        ) as cursor:
            rowcount = cursor.rowcount

        if rowcount == 0:
            return

        # Stop the log writer before tearing the Docker container down so
        # the final captured chunks are persisted (directory is preserved
        # for post-mortem of the failed init).
        try:
            await self._logs.stop(worker_id)
        except Exception:
            logger.exception(
                "Failed to stop log capture for worker %s", worker_id
            )

        try:
            await self._sockets.destroy_socket(worker_id)
        except Exception:
            logger.exception(
                "Failed to destroy socket for worker %s", worker_id
            )

        if docker_id:
            try:
                await self._docker.remove_worker(docker_id, force=True)
            except Exception:
                logger.exception(
                    "Failed to force-remove Docker container %s for %s",
                    docker_id,
                    worker_id,
                )

    # -- list ---------------------------------------------------------------

    async def list_workers(self) -> list[WorkerResponse]:
        """Return all workers, newest first.

        Reads straight from the DB without per-row Docker reconciliation;
        ``get_worker`` syncs an individual row, and the reaper handles
        background drift.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM workers ORDER BY created_at DESC"
        )
        return [_row_to_response(row) for row in rows]

    # -- get ----------------------------------------------------------------

    async def get_worker(self, worker_id: str) -> WorkerResponse:
        row = await self._db.fetchone(
            "SELECT * FROM workers WHERE id = ?", (worker_id,)
        )
        if not row:
            raise WorkerNotFound(worker_id)

        # Sync with Docker unless the worker is in a terminal state or
        # still initializing (the background task owns state transitions for
        # the latter, and docker_id may still be NULL).
        current_status = row["status"]
        if current_status not in ("destroyed", "error", "initializing"):
            try:
                inspection = await self._docker.inspect_worker(row["docker_id"])
                mapped = _docker_state_to_status(inspection)
                # Only update if Docker disagrees AND we're not mid-transition
                if (
                    mapped
                    and mapped.value != current_status
                    and current_status
                    not in ("stopping", "resuming", "destroying")
                ):
                    await self._db.execute_insert(
                        "UPDATE workers SET status = ? WHERE id = ?",
                        (mapped.value, worker_id),
                    )
                    logger.info(
                        "Worker %s status synced: %s -> %s",
                        worker_id,
                        current_status,
                        mapped.value,
                    )
                    row = await self._db.fetchone(
                        "SELECT * FROM workers WHERE id = ?", (worker_id,)
                    )
            except WorkerNotFoundError:
                # Docker container gone — mark as destroyed
                await self._db.execute_insert(
                    "UPDATE workers SET status = 'destroyed' WHERE id = ?",
                    (worker_id,),
                )
                logger.warning(
                    "Worker %s not found in Docker, marked destroyed",
                    worker_id,
                )
                row = await self._db.fetchone(
                    "SELECT * FROM workers WHERE id = ?", (worker_id,)
                )

        return _row_to_response(row)

    # -- stop ---------------------------------------------------------------

    async def stop_worker(self, worker_id: str) -> WorkerResponse:
        row = await self._db.fetchone(
            "SELECT * FROM workers WHERE id = ?", (worker_id,)
        )
        if not row:
            raise WorkerNotFound(worker_id)
        if row["status"] != "running":
            raise WorkerStateConflict(worker_id, row["status"], "stop")

        await self._db.execute_insert(
            "UPDATE workers SET status = 'stopping' WHERE id = ?",
            (worker_id,),
        )

        # Close the socket connection but preserve the socket file for resume
        await self._sockets.close_socket(worker_id)

        try:
            await self._docker.stop_worker(row["docker_id"])
        except WorkerNotFoundError:
            pass  # Already gone

        # The follow stream is closing in the background; await the writer
        # task here so .cursor is persisted before stopped is observable.
        try:
            await self._logs.stop(worker_id)
        except Exception:
            logger.exception(
                "Failed to stop log capture for worker %s", worker_id
            )

        now = _now_iso()
        await self._db.execute_insert(
            "UPDATE workers SET status = 'stopped', stopped_at = ? WHERE id = ?",
            (now, worker_id),
        )

        logger.info("Stopped worker %s", worker_id)
        row = await self._db.fetchone(
            "SELECT * FROM workers WHERE id = ?", (worker_id,)
        )
        return _row_to_response(row, _DOCKER_STOP_TIMEOUT)

    # -- resume -------------------------------------------------------------

    async def resume_worker(self, worker_id: str) -> WorkerResponse:
        """Begin resuming a stopped worker.

        Returns immediately with status ``resuming``.  The Docker start and
        the wait for the worker agent's ``ready`` message run in the
        background, mirroring the ``initializing`` flow.  The worker
        transitions to ``running`` only when ``ready`` arrives.  A
        watchdog transitions the row to ``error`` (code ``resume_timeout``)
        if ``ready`` does not arrive within ``init_timeout_seconds``.
        """
        row = await self._db.fetchone(
            "SELECT * FROM workers WHERE id = ?", (worker_id,)
        )
        if not row:
            raise WorkerNotFound(worker_id)
        if row["status"] != "stopped":
            raise WorkerStateConflict(worker_id, row["status"], "resume")

        await self._db.execute_insert(
            "UPDATE workers SET status = 'resuming' WHERE id = ?",
            (worker_id,),
        )

        resume_task = asyncio.create_task(
            self._resume_worker_async(worker_id, row["docker_id"])
        )
        self._init_tasks[worker_id] = resume_task
        resume_task.add_done_callback(
            lambda _t, wid=worker_id: self._init_tasks.pop(wid, None)
        )

        watchdog_task = asyncio.create_task(
            self._init_watchdog(worker_id, resume_task, "resuming", "resume_timeout")
        )
        self._init_watchdogs[worker_id] = watchdog_task
        watchdog_task.add_done_callback(
            lambda _t, wid=worker_id: self._init_watchdogs.pop(wid, None)
        )

        logger.info("Resuming worker %s; awaiting ready", worker_id)
        row = await self._db.fetchone(
            "SELECT * FROM workers WHERE id = ?", (worker_id,)
        )
        return _row_to_response(row, self._config.init_timeout_seconds)

    async def _resume_worker_async(
        self, worker_id: str, docker_id: str
    ) -> None:
        """Background work for resume: re-create socket, docker start, log capture.

        Mirrors ``_init_worker`` but for an existing Docker container.
        On Docker failure, transitions the row to ``error`` with code
        ``resume_docker_error``.  On success, the row stays ``resuming``
        until the worker agent sends ``ready`` (handled by SocketManager).
        """
        try:
            # Re-create the socket listener before starting the container so
            # the agent has something to connect to when it boots back up.
            await self._sockets.create_socket(worker_id)

            # Read the cursor before starting so log capture resumes from
            # the last captured timestamp.  None when no logs have been
            # captured yet (e.g. capture disabled on first run).
            cursor = self._logs.read_cursor(worker_id)

            await self._docker.start_worker(docker_id)

            try:
                await self._logs.start(worker_id, docker_id, since=cursor)
            except Exception:
                logger.exception(
                    "Worker %s: log capture failed to resume", worker_id
                )

            logger.info(
                "Worker %s Docker resume complete (docker=%s); awaiting ready",
                worker_id,
                docker_id[:12],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Worker %s failed during Docker resume", worker_id
            )
            await self._fail_init(
                worker_id, "resume_docker_error", docker_id, "resuming"
            )

    # -- destroy ------------------------------------------------------------

    async def destroy_worker(self, worker_id: str) -> WorkerResponse:
        row = await self._db.fetchone(
            "SELECT * FROM workers WHERE id = ?", (worker_id,)
        )
        if not row:
            raise WorkerNotFound(worker_id)
        if row["status"] == "destroyed":
            raise WorkerStateConflict(worker_id, row["status"], "destroy")

        await self._db.execute_insert(
            "UPDATE workers SET status = 'destroying' WHERE id = ?",
            (worker_id,),
        )

        # Close and remove the socket, then stop and remove the Docker container
        await self._sockets.destroy_socket(worker_id)

        docker_id = row["docker_id"]
        # docker_id may be NULL for workers that failed init before the
        # Docker create call completed (status 'error').
        if docker_id:
            try:
                await self._docker.stop_worker(docker_id)
            except (WorkerNotFoundError, WorkerConflictError):
                pass  # Already stopped or gone

            try:
                await self._docker.remove_worker(docker_id, force=True)
            except WorkerNotFoundError:
                pass  # Already removed

        # Drop the captured logs along with the rest of the worker's
        # state — applies to errored and initializing rows too.
        try:
            await self._logs.discard(worker_id)
        except Exception:
            logger.exception(
                "Failed to discard log capture for worker %s", worker_id
            )

        # Preserve existing stopped_at if already set (worker was stopped
        # before being destroyed), otherwise record the current time.
        stopped_at = row["stopped_at"] or _now_iso()
        await self._db.execute_insert(
            "UPDATE workers SET status = 'destroyed', stopped_at = ? WHERE id = ?",
            (stopped_at, worker_id),
        )

        logger.info("Destroyed worker %s", worker_id)
        row = await self._db.fetchone(
            "SELECT * FROM workers WHERE id = ?", (worker_id,)
        )
        return _row_to_response(row, _DOCKER_STOP_TIMEOUT)

    # -- exec ---------------------------------------------------------------

    async def _assert_capability(self, image_name: str, capability: str) -> None:
        """Deny the operation unless the worker's image declares ``capability``.

        Resolves the worker's stored image short name to its Drover labels
        and parses ``drover.capabilities``.  An absent/empty label, or an
        image that can no longer be resolved (deleted/renamed since launch),
        means the capability is unavailable — deny rather than assume.
        """
        matches = await self._docker.list_images(name=image_name)
        if not matches:
            raise CapabilityNotSupported(capability)
        labels = matches[0].get("Labels") or {}
        if capability not in parse_capabilities(labels.get(CAPABILITIES_LABEL)):
            raise CapabilityNotSupported(capability)

    async def exec_command(self, worker_id: str, command: str) -> str:
        """Send a command to a running worker.  Returns the command ID."""
        row = await self._db.fetchone(
            "SELECT * FROM workers WHERE id = ?", (worker_id,)
        )
        if not row:
            raise WorkerNotFound(worker_id)
        if row["status"] != "running":
            raise WorkerStateConflict(worker_id, row["status"], "exec on")

        if not self._sockets.is_connected(worker_id):
            raise WorkerNotConnected(worker_id)

        await self._assert_capability(row["image"], CAPABILITY_EXEC)

        command_id = generate_id()
        now = _now_iso()

        await self._db.execute_insert(
            "INSERT INTO commands (id, worker_id, command, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (command_id, worker_id, command, now),
        )

        await self._sockets.send_command(worker_id, command_id, command)

        logger.info(
            "Exec command %s on worker %s: %s",
            command_id,
            worker_id,
            command[:80],
        )
        return command_id

    async def list_commands(self, worker_id: str) -> list[CommandSummary]:
        """Return all commands for a worker, newest first."""
        worker = await self._db.fetchone(
            "SELECT id FROM workers WHERE id = ?", (worker_id,)
        )
        if not worker:
            raise WorkerNotFound(worker_id)

        rows = await self._db.fetchall(
            "SELECT id, command, status, exit_code, created_at FROM commands "
            "WHERE worker_id = ? ORDER BY created_at DESC",
            (worker_id,),
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
        self, worker_id: str, command_id: str
    ) -> ExecStatusResponse:
        """Get the status and output of a command."""
        # Verify worker exists
        worker = await self._db.fetchone(
            "SELECT id FROM workers WHERE id = ?", (worker_id,)
        )
        if not worker:
            raise WorkerNotFound(worker_id)

        # Fetch command
        cmd_row = await self._db.fetchone(
            "SELECT * FROM commands WHERE id = ? AND worker_id = ?",
            (command_id, worker_id),
        )
        if not cmd_row:
            raise CommandNotFound(worker_id, command_id)

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
