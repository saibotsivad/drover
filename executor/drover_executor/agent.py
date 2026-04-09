"""Core guest agent that connects to the orchestrator's Unix socket."""

import asyncio
import logging
import signal

from drover_executor import protocol
from drover_executor.runner import run_command

logger = logging.getLogger(__name__)


class Agent:
    """Guest agent that runs inside a Drover container.

    Connects to the orchestrator's Unix socket, receives commands, executes
    them as subprocesses, streams stdout/stderr back, and reports exit codes.

    Subclass and override ``on_connect``, ``on_command``, or ``on_disconnect``
    to customise behaviour.
    """

    def __init__(
        self,
        socket_path: str = "/run/orchestrator.sock",
        heartbeat_interval: float = 2.0,
        max_concurrent_commands: int | None = None,
        auto_heartbeat: bool = True,
    ) -> None:
        self.socket_path = socket_path
        self.heartbeat_interval = heartbeat_interval
        self.auto_heartbeat = auto_heartbeat

        self._write_lock = asyncio.Lock()
        self._writer: asyncio.StreamWriter | None = None
        self._command_tasks: dict[str, asyncio.Task] = {}
        self._heartbeat_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()

        self._semaphore: asyncio.Semaphore | None = None
        if max_concurrent_commands is not None:
            self._semaphore = asyncio.Semaphore(max_concurrent_commands)

    # -- public API ----------------------------------------------------------

    async def run(self) -> None:
        """Connect to the orchestrator socket and process messages."""
        loop = asyncio.get_running_loop()

        installed_signals: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
                installed_signals.append(sig)
            except (OSError, RuntimeError):
                # Not the main thread, or signal not supported — skip.
                pass

        logger.info("Connecting to %s", self.socket_path)
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        self._writer = writer

        try:
            await self.on_connect()

            if self.auto_heartbeat:
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            await self._read_loop(reader)
        finally:
            await self._shutdown(writer)
            for sig in installed_signals:
                loop.remove_signal_handler(sig)

    async def send_done(self) -> None:
        """Send a done signal to the orchestrator."""
        await self._send(protocol.encode_done())

    async def send_heartbeat(self) -> None:
        """Send a single heartbeat.  Useful when ``auto_heartbeat=False``."""
        await self._send(protocol.encode_heartbeat())

    # -- override points -----------------------------------------------------

    async def on_connect(self) -> None:
        """Called after the socket connection is established."""

    async def on_command(self, cmd_id: str, exec_str: str) -> None:
        """Called for each incoming command.

        The default implementation runs the command as a subprocess and streams
        its output back to the orchestrator.  Override to customise.
        """
        async def send_output(stream: str, data: str) -> None:
            await self._send(protocol.encode_output(cmd_id, stream, data))

        exit_code = await run_command(exec_str, send_output)
        await self._send(protocol.encode_result(cmd_id, exit_code))

    async def on_disconnect(self) -> None:
        """Called after the socket connection is closed."""

    # -- internals -----------------------------------------------------------

    async def _send(self, data: bytes) -> None:
        """Write data to the socket with serialization lock."""
        async with self._write_lock:
            if self._writer is None:
                return
            self._writer.write(data)
            await self._writer.drain()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Read and dispatch messages until the connection closes or shutdown."""
        read_task: asyncio.Task | None = None
        shutdown_task: asyncio.Task | None = None
        try:
            while not self._shutdown_event.is_set():
                read_task = asyncio.create_task(reader.readline())
                shutdown_task = asyncio.create_task(self._shutdown_event.wait())

                done, _ = await asyncio.wait(
                    {read_task, shutdown_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                shutdown_task.cancel()

                if read_task in done:
                    line = read_task.result()
                    read_task = None
                    if not line:
                        logger.info("Connection closed by orchestrator")
                        break
                    self._dispatch(line)
                else:
                    # shutdown was requested
                    read_task.cancel()
                    try:
                        await read_task
                    except asyncio.CancelledError:
                        pass
                    read_task = None
                    break
        except asyncio.CancelledError:
            if read_task and not read_task.done():
                read_task.cancel()
                try:
                    await read_task
                except asyncio.CancelledError:
                    pass
            if shutdown_task and not shutdown_task.done():
                shutdown_task.cancel()
                try:
                    await shutdown_task
                except asyncio.CancelledError:
                    pass
            raise

    def _dispatch(self, line: bytes) -> None:
        """Parse a message and dispatch command messages to tasks."""
        try:
            msg = protocol.decode(line)
        except ValueError:
            logger.warning("Invalid message: %r", line)
            return

        if msg.get("type") == "command":
            cmd_id = msg["id"]
            exec_str = msg["exec"]
            task = asyncio.create_task(self._run_command(cmd_id, exec_str))
            self._command_tasks[cmd_id] = task
            task.add_done_callback(lambda t, cid=cmd_id: self._command_tasks.pop(cid, None))
        else:
            logger.debug("Ignoring message type: %s", msg.get("type"))

    async def _run_command(self, cmd_id: str, exec_str: str) -> None:
        """Run a single command, respecting the concurrency semaphore."""
        try:
            if self._semaphore:
                async with self._semaphore:
                    await self.on_command(cmd_id, exec_str)
            else:
                await self.on_command(cmd_id, exec_str)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error running command %s", cmd_id)

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats for the lifetime of the connection."""
        try:
            while True:
                await self._send(protocol.encode_heartbeat())
                await asyncio.sleep(self.heartbeat_interval)
        except asyncio.CancelledError:
            pass

    def _request_shutdown(self) -> None:
        """Signal handler: request a graceful shutdown."""
        logger.info("Shutdown signal received")
        self._shutdown_event.set()

    async def _shutdown(self, writer: asyncio.StreamWriter) -> None:
        """Cancel running tasks and close the connection."""
        # Cancel heartbeat
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Cancel all running commands
        for task in list(self._command_tasks.values()):
            if not task.done():
                task.cancel()
        if self._command_tasks:
            await asyncio.gather(
                *self._command_tasks.values(), return_exceptions=True
            )

        # Send done signal (best-effort)
        try:
            await self.send_done()
        except Exception:
            pass

        await self.on_disconnect()

        # Close the writer
        self._writer = None
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

        logger.info("Agent shut down")
