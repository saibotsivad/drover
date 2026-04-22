"""Per-container Unix socket lifecycle and message routing.

Each running container gets a Unix socket at {SOCKET_DIR}/{container_id}.sock.
The guest agent inside the container connects to this socket and communicates
using newline-delimited JSON messages.

Message types (guest -> orchestrator):
  - ready:     {"type": "ready"}
  - heartbeat: {"type": "heartbeat"}
  - output:    {"type": "output", "id": "<cmd_id>", "stream": "stdout|stderr", "data": "..."}
  - result:    {"type": "result", "id": "<cmd_id>", "exit_code": N}
  - done:      {"type": "done"}

Message types (orchestrator -> guest):
  - command:   {"type": "command", "id": "<cmd_id>", "exec": "..."}
"""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from orchestrator.config import Config
from orchestrator.database import Database

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

    def set_done_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register a callback invoked when a container sends a done signal."""
        self._done_callback = callback

    def set_ready_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register a callback invoked after a ready transition succeeds.

        Fired only when the ``ready`` message actually transitions the row
        from ``initializing`` to ``running``.  The orchestrator uses this
        to cancel the init timeout watchdog for the container.
        """
        self._ready_callback = callback

    async def create_socket(self, container_id: str) -> str:
        """Create a Unix socket for a container and start listening.

        Returns the socket path.  Must be called BEFORE the Docker container
        starts so the socket file exists for the bind mount.
        """
        socket_path = os.path.join(self._config.socket_dir, f"{container_id}.sock")
        os.makedirs(self._config.socket_dir, exist_ok=True)

        # Remove stale socket file if present (e.g. from a previous run)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass

        server = await asyncio.start_unix_server(
            lambda r, w: self._handle_connection(container_id, r, w),
            path=socket_path,
        )
        # Make socket world-writable so the container process can connect
        os.chmod(socket_path, 0o777)

        self._servers[container_id] = server
        logger.info("Socket created for container %s at %s", container_id, socket_path)
        return socket_path

    async def close_socket(self, container_id: str) -> None:
        """Close the socket connection but keep the socket file (for resume)."""
        await self._cleanup_connection(container_id)

        server = self._servers.pop(container_id, None)
        if server:
            server.close()
            await server.wait_closed()

        logger.info("Socket closed for container %s (file preserved)", container_id)

    async def destroy_socket(self, container_id: str) -> None:
        """Close the socket connection AND remove the socket file."""
        await self._cleanup_connection(container_id)

        server = self._servers.pop(container_id, None)
        if server:
            server.close()
            await server.wait_closed()

        socket_path = os.path.join(self._config.socket_dir, f"{container_id}.sock")
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass

        logger.info("Socket destroyed for container %s", container_id)

    async def send_command(
        self, container_id: str, command_id: str, exec_str: str
    ) -> None:
        """Send a command to the guest agent via the socket."""
        writer = self._writers.get(container_id)
        if writer is None:
            raise RuntimeError(
                f"No active connection for container '{container_id}'"
            )

        msg = json.dumps({"type": "command", "id": command_id, "exec": exec_str})
        writer.write((msg + "\n").encode())
        await writer.drain()
        logger.debug("Sent command %s to container %s", command_id, container_id)

    async def close_all(self) -> None:
        """Shut down all sockets.  Called during app shutdown."""
        container_ids = list(self._servers.keys())
        for cid in container_ids:
            await self.close_socket(cid)

    def is_connected(self, container_id: str) -> bool:
        """Check if a guest agent is currently connected for this container."""
        return container_id in self._writers

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
        container_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a new guest agent connection.

        Only one connection per container is expected.  If a second
        connection arrives, the previous one is closed first.
        """
        # Close any previous connection for this container
        await self._cleanup_connection(container_id)

        self._writers[container_id] = writer
        current_task = asyncio.current_task()
        self._tasks[container_id] = current_task

        logger.info("Guest agent connected for container %s", container_id)

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # Connection closed

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Invalid JSON from container %s: %r", container_id, line
                    )
                    continue

                await self._handle_message(container_id, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error reading from container %s", container_id)
        finally:
            # Only clean up if we are still the active connection
            if self._writers.get(container_id) is writer:
                self._writers.pop(container_id, None)
            if self._tasks.get(container_id) is current_task:
                self._tasks.pop(container_id, None)
            logger.info("Guest agent disconnected for container %s", container_id)

    async def _handle_message(self, container_id: str, msg: dict) -> None:
        """Route a message from the guest agent."""
        msg_type = msg.get("type")

        if msg_type == "ready":
            await self._handle_ready(container_id)
        elif msg_type == "heartbeat":
            await self._handle_heartbeat(container_id)
        elif msg_type == "output":
            await self._handle_output(msg)
        elif msg_type == "result":
            await self._handle_result(msg)
        elif msg_type == "done":
            await self._handle_done(container_id)
        else:
            logger.warning(
                "Unknown message type from container %s: %s",
                container_id,
                msg_type,
            )

    async def _handle_ready(self, container_id: str) -> None:
        """Transition an initializing container to running.

        The UPDATE is conditional on the current status being
        ``initializing`` so a late-arriving ready (e.g. after the init
        watchdog has already fired and transitioned the row to ``error``)
        is silently ignored.  Only a successful transition fires the ready
        callback so the orchestrator can cancel the watchdog.
        """
        async with self._db.execute(
            "UPDATE containers SET status = 'running' "
            "WHERE id = ? AND status = 'initializing'",
            (container_id,),
        ) as cursor:
            rowcount = cursor.rowcount

        if rowcount == 0:
            logger.debug(
                "Ignored ready from container %s (not in initializing state)",
                container_id,
            )
            return

        logger.info("Container %s ready; status initializing -> running", container_id)
        if self._ready_callback:
            asyncio.create_task(self._ready_callback(container_id))

    async def _handle_heartbeat(self, container_id: str) -> None:
        now = _now_iso()
        await self._db.execute_insert(
            "UPDATE containers SET last_seen = ? WHERE id = ?",
            (now, container_id),
        )
        logger.debug("Heartbeat from container %s", container_id)

    async def _handle_output(self, msg: dict) -> None:
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

        logger.debug(
            "Output for command %s: [%s] %d bytes", command_id, stream, len(data)
        )

    async def _handle_result(self, msg: dict) -> None:
        command_id = msg.get("id")
        exit_code = msg.get("exit_code")

        await self._db.execute_insert(
            "UPDATE commands SET status = 'complete', exit_code = ? WHERE id = ?",
            (exit_code, command_id),
        )

        logger.info(
            "Command %s completed with exit_code=%s", command_id, exit_code
        )

    async def _handle_done(self, container_id: str) -> None:
        logger.info("Done signal from container %s", container_id)
        if self._done_callback:
            asyncio.create_task(self._done_callback(container_id))
