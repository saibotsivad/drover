"""Per-worker Unix socket lifecycle and message routing.

Each running worker gets its own folder
``/var/run/drover/sockets/{worker_id}/`` (fixed in-container path, see
orchestrator/config.py) containing the orchestrator control socket
``orchestrator.sock``.  The whole folder is bind-mounted into the
worker at ``/var/run/drover/sockets/`` so the worker agent
connects to ``/var/run/drover/sockets/orchestrator.sock``.  The folder
layout (rather than a single socket file) leaves room for additional
per-worker sockets later, e.g. one per interactive shell.

The worker agent inside the container connects to this socket and communicates
using newline-delimited JSON messages.

Message types (worker agent -> orchestrator):
  - ready:     {"type": "ready"}
  - heartbeat: {"type": "heartbeat"}
  - output:    {"type": "output", "id": "<cmd_id>", "stream": "stdout|stderr", "data": "..."}
  - result:    {"type": "result", "id": "<cmd_id>", "exit_code": N}
  - done:      {"type": "done"}

Message types (orchestrator -> worker agent):
  - command:   {"type": "command", "id": "<cmd_id>", "exec": "..."}
"""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from orchestrator.config import ORCHESTRATOR_SOCKET_NAME, Config
from orchestrator.database import Database

if TYPE_CHECKING:
    from orchestrator.connection_manager import ConnectionManager

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SocketManager:
    def __init__(self, config: Config, db: Database) -> None:
        self._config = config
        self._db = db
        self._servers: dict[str, asyncio.AbstractServer] = {}
        self._writers: dict[str, asyncio.StreamWriter] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._done_callback: Callable[[str], Awaitable[None]] | None = None
        self._ready_callback: Callable[[str], Awaitable[None]] | None = None
        self._connection_manager: "ConnectionManager | None" = None

    def set_connection_manager(self, cm: "ConnectionManager") -> None:
        """Wire up the ConnectionManager after it is created.

        Output and result messages from worker agents are broadcast
        through it to any WebSocket clients subscribed to the
        worker.
        """
        self._connection_manager = cm

    def set_done_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register a callback invoked when a worker sends a done signal."""
        self._done_callback = callback

    def set_ready_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register a callback invoked after a ready transition succeeds.

        Fired only when the ``ready`` message actually transitions the row
        from ``initializing`` or ``resuming`` to ``running``.  The orchestrator
        uses this to cancel the init/resume timeout watchdog for the
        worker.
        """
        self._ready_callback = callback

    def _worker_dir(self, worker_id: str) -> str:
        """Per-worker socket folder bind-mounted into the worker."""
        return os.path.join(self._config.socket_dir, worker_id)

    def _socket_path(self, worker_id: str) -> str:
        """Path to the orchestrator control socket inside the worker folder."""
        return os.path.join(self._worker_dir(worker_id), ORCHESTRATOR_SOCKET_NAME)

    async def create_socket(self, worker_id: str) -> str:
        """Create a Unix socket for a worker and start listening.

        Returns the socket path.  Must be called BEFORE the Docker container
        starts so the per-worker folder and socket file exist for the
        bind mount.
        """
        worker_dir = self._worker_dir(worker_id)
        socket_path = self._socket_path(worker_id)
        os.makedirs(worker_dir, exist_ok=True)

        # Remove stale socket file if present (e.g. from a previous run)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass

        server = await asyncio.start_unix_server(
            lambda r, w: self._handle_connection(worker_id, r, w),
            path=socket_path,
        )
        # Make socket world-writable so the container process can connect
        os.chmod(socket_path, 0o777)

        self._servers[worker_id] = server
        logger.info("Socket created for worker %s at %s", worker_id, socket_path)
        return socket_path

    async def close_socket(self, worker_id: str) -> None:
        """Close the socket connection but keep the socket file (for resume)."""
        await self._cleanup_connection(worker_id)

        server = self._servers.pop(worker_id, None)
        if server:
            server.close()
            await server.wait_closed()

        logger.info("Socket closed for worker %s (file preserved)", worker_id)

    async def destroy_socket(self, worker_id: str) -> None:
        """Close the socket connection AND remove the socket file."""
        await self._cleanup_connection(worker_id)

        server = self._servers.pop(worker_id, None)
        if server:
            server.close()
            await server.wait_closed()

        try:
            os.unlink(self._socket_path(worker_id))
        except FileNotFoundError:
            pass
        # Remove the now-empty per-worker folder.
        try:
            os.rmdir(self._worker_dir(worker_id))
        except (FileNotFoundError, OSError):
            pass

        logger.info("Socket destroyed for worker %s", worker_id)

    async def send_command(
        self, worker_id: str, command_id: str, exec_str: str
    ) -> None:
        """Send a command to the worker agent via the socket."""
        writer = self._writers.get(worker_id)
        if writer is None:
            raise RuntimeError(
                f"No active connection for worker '{worker_id}'"
            )

        msg = json.dumps({"type": "command", "id": command_id, "exec": exec_str})
        writer.write((msg + "\n").encode())
        await writer.drain()
        logger.debug("Sent command %s to worker %s", command_id, worker_id)

    async def close_all(self) -> None:
        """Shut down all sockets.  Called during app shutdown."""
        worker_ids = list(self._servers.keys())
        for wid in worker_ids:
            await self.close_socket(wid)

    def is_connected(self, worker_id: str) -> bool:
        """Check if a worker agent is currently connected for this worker."""
        return worker_id in self._writers

    # -- internal helpers ----------------------------------------------------

    async def _cleanup_connection(self, container_id: str) -> None:
        """Cancel the reader task and close the writer for a container."""
        task = self._tasks.pop(container_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        writer = self._writers.pop(container_id, None)
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_connection(
        self,
        worker_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a new worker agent connection.

        Only one connection per worker is expected.  If a second
        connection arrives, the previous one is closed first.
        """
        # Close any previous connection for this worker
        await self._cleanup_connection(worker_id)

        self._writers[worker_id] = writer
        current_task = asyncio.current_task()
        self._tasks[worker_id] = current_task

        logger.info("Worker agent connected for worker %s", worker_id)

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # Connection closed

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Invalid JSON from worker %s: %r", worker_id, line
                    )
                    continue

                await self._handle_message(worker_id, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error reading from worker %s", worker_id)
        finally:
            # Only clean up if we are still the active connection
            if self._writers.get(worker_id) is writer:
                self._writers.pop(worker_id, None)
            if self._tasks.get(worker_id) is current_task:
                self._tasks.pop(worker_id, None)
            logger.info("Worker agent disconnected for worker %s", worker_id)

    async def _handle_message(self, worker_id: str, msg: dict) -> None:
        """Route a message from the worker agent."""
        msg_type = msg.get("type")

        if msg_type == "ready":
            await self._handle_ready(worker_id)
        elif msg_type == "heartbeat":
            await self._handle_heartbeat(worker_id)
        elif msg_type == "output":
            await self._handle_output(worker_id, msg)
        elif msg_type == "result":
            await self._handle_result(worker_id, msg)
        elif msg_type == "done":
            await self._handle_done(worker_id)
        else:
            logger.warning(
                "Unknown message type from worker %s: %s",
                worker_id,
                msg_type,
            )

    async def _handle_ready(self, worker_id: str) -> None:
        """Transition an initializing or resuming worker to running.

        The UPDATE is conditional on the current status being
        ``initializing`` or ``resuming`` so a late-arriving ready (e.g.
        after the watchdog has already fired and transitioned the row to
        ``error``) is silently ignored.  Both paths share this gate
        because the worker agent's connect-then-send-ready handshake is
        identical for first init and for resume after stop.  Only a
        successful transition fires the ready callback so the
        orchestrator can cancel the watchdog.
        """
        # Capture the source status before the update so we can log
        # whether this was an init or a resume transition.
        row = await self._db.fetchone(
            "SELECT status FROM workers WHERE id = ?", (worker_id,)
        )
        source_status = row["status"] if row else None

        async with self._db.execute(
            "UPDATE workers SET status = 'running', stopped_at = NULL "
            "WHERE id = ? AND status IN ('initializing', 'resuming')",
            (worker_id,),
        ) as cursor:
            rowcount = cursor.rowcount

        if rowcount == 0:
            logger.debug(
                "Ignored ready from worker %s (status=%s)",
                worker_id,
                source_status,
            )
            return

        logger.info(
            "Worker %s ready; status %s -> running",
            worker_id,
            source_status,
        )
        if self._ready_callback:
            asyncio.create_task(self._ready_callback(worker_id))

    async def _handle_heartbeat(self, worker_id: str) -> None:
        now = _now_iso()
        await self._db.execute_insert(
            "UPDATE workers SET last_seen = ? WHERE id = ?",
            (now, worker_id),
        )
        logger.debug("Heartbeat from worker %s", worker_id)

    async def _handle_output(self, worker_id: str, msg: dict) -> None:
        command_id = msg.get("id")
        stream = msg.get("stream", "stdout")
        data = msg.get("data", "")
        now = _now_iso()

        await self._db.execute_insert(
            "INSERT INTO command_messages (command_id, stream, data, received_at) "
            "VALUES (?, ?, ?, ?)",
            (command_id, stream, data, now),
        )

        # On first output, transition from pending -> running
        await self._db.execute_insert(
            "UPDATE commands SET status = 'running' "
            "WHERE id = ? AND status = 'pending'",
            (command_id,),
        )

        if self._connection_manager is not None:
            await self._connection_manager.broadcast(
                worker_id,
                {
                    "type": "output",
                    "command_id": command_id,
                    "stream": stream,
                    "data": data,
                },
            )

        logger.debug(
            "Output for command %s: [%s] %d bytes", command_id, stream, len(data)
        )

    async def _handle_result(self, worker_id: str, msg: dict) -> None:
        command_id = msg.get("id")
        exit_code = msg.get("exit_code")

        await self._db.execute_insert(
            "UPDATE commands SET status = 'complete', exit_code = ? WHERE id = ?",
            (exit_code, command_id),
        )

        if self._connection_manager is not None:
            await self._connection_manager.broadcast(
                worker_id,
                {
                    "type": "status",
                    "command_id": command_id,
                    "status": "complete",
                    "exit_code": exit_code,
                },
            )

        logger.info(
            "Command %s completed with exit_code=%s", command_id, exit_code
        )

    async def _handle_done(self, worker_id: str) -> None:
        logger.info("Done signal from worker %s", worker_id)
        if self._done_callback:
            asyncio.create_task(self._done_callback(worker_id))
