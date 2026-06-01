"""Tests for drover_executor.agent — full lifecycle against a mock socket server.

Each test creates a real Unix socket server (via tmp_path) and runs the Agent
against it.  The mock server speaks the same newline-delimited JSON protocol as
the orchestrator.

NOTE: Python 3.12 changed ``Server.wait_closed()`` to wait for *all* accepted
connections to close, not just the listening socket.  Every test must close the
server-side writer (from ``on_connect``) before calling ``server.wait_closed()``
or the test will hang.
"""

import asyncio
import json
import os

import pytest

from drover_executor.agent import Agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _socket_path(tmp_path) -> str:
    return str(tmp_path / "test.sock")


async def _read_message(reader: asyncio.StreamReader) -> dict:
    """Read a single newline-delimited JSON message from the stream."""
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    assert line, "Unexpected EOF from agent"
    return json.loads(line)


async def _send_command(
    writer: asyncio.StreamWriter, cmd_id: str, exec_str: str
) -> None:
    msg = json.dumps({"type": "command", "id": cmd_id, "exec": exec_str})
    writer.write((msg + "\n").encode())
    await writer.drain()


async def _collect_messages_until_result(
    reader: asyncio.StreamReader,
    cmd_id: str,
    timeout: float = 10,
) -> list[dict]:
    """Collect messages until we see a result for the given command."""
    messages = []
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        line = await asyncio.wait_for(reader.readline(), timeout=remaining)
        if not line:
            break
        msg = json.loads(line)
        messages.append(msg)
        if msg.get("type") == "result" and msg.get("id") == cmd_id:
            break
    return messages


async def _shutdown_test(agent_task, reader_holder, server):
    """Common cleanup: cancel agent, close server-side conn, close server.

    Python 3.12 requires the server-side writer to be closed before
    ``server.wait_closed()`` returns.
    """
    agent_task.cancel()
    try:
        await agent_task
    except asyncio.CancelledError:
        pass
    if "w" in reader_holder:
        reader_holder["w"].close()
    server.close()
    await server.wait_closed()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAgentConnection:
    async def test_ready_sent_before_heartbeat(self, tmp_path):
        """The first message from the agent is always ``ready``.

        The Agent base class emits ``ready`` immediately after
        ``on_connect()`` returns so the orchestrator can transition the
        worker from ``initializing`` to ``running``.
        """
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        agent = Agent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]

            msg = await _read_message(reader)
            assert msg == {"type": "ready"}
        finally:
            await _shutdown_test(agent_task, reader_holder, server)

    async def test_ready_sent_after_on_connect_completes(self, tmp_path):
        """Ready is sent only after ``on_connect()`` finishes its work."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        startup_order: list[str] = []

        class SlowAgent(Agent):
            async def on_connect(self) -> None:
                await asyncio.sleep(0.1)
                startup_order.append("on_connect_done")

        agent = SlowAgent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]

            msg = await _read_message(reader)
            startup_order.append("ready_received")
            assert msg == {"type": "ready"}
            # on_connect ran to completion first, then ready was emitted
            assert startup_order == ["on_connect_done", "ready_received"]
        finally:
            await _shutdown_test(agent_task, reader_holder, server)

    async def test_connect_and_heartbeat(self, tmp_path):
        """Agent connects and sends heartbeats after the initial ready."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        agent = Agent(socket_path=path, heartbeat_interval=0.1)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]

            # Consume the initial ready message
            msg = await _read_message(reader)
            assert msg["type"] == "ready"

            # Then heartbeats flow
            msg = await _read_message(reader)
            assert msg["type"] == "heartbeat"

            msg2 = await _read_message(reader)
            assert msg2["type"] == "heartbeat"
        finally:
            await _shutdown_test(agent_task, reader_holder, server)

    async def test_no_auto_heartbeat(self, tmp_path):
        """When auto_heartbeat=False, no heartbeats are sent automatically."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        agent = Agent(
            socket_path=path,
            heartbeat_interval=0.05,
            auto_heartbeat=False,
        )
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]

            # Wait enough time for several heartbeats to have been sent
            await asyncio.sleep(0.2)

            # Send a command to produce a message we can detect
            writer = reader_holder["w"]
            await _send_command(writer, "probe", "echo probe")

            messages = await _collect_messages_until_result(reader, "probe")
            # None of the messages before the probe result should be heartbeats
            types_before = [m["type"] for m in messages if m.get("id") != "probe"]
            assert "heartbeat" not in types_before
        finally:
            await _shutdown_test(agent_task, reader_holder, server)


class TestAgentCommandExecution:
    async def test_simple_command(self, tmp_path):
        """Agent executes a command and returns output + result."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        agent = Agent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]
            writer = reader_holder["w"]

            await _send_command(writer, "cmd-1", "echo 'hello world'")

            messages = await _collect_messages_until_result(reader, "cmd-1")
            non_hb = [m for m in messages if m["type"] != "heartbeat"]

            # Should have at least one output and one result
            output_msgs = [m for m in non_hb if m["type"] == "output"]
            result_msgs = [m for m in non_hb if m["type"] == "result"]

            assert len(output_msgs) >= 1
            assert output_msgs[0]["id"] == "cmd-1"
            assert output_msgs[0]["stream"] == "stdout"
            assert "hello world" in output_msgs[0]["data"]

            assert len(result_msgs) == 1
            assert result_msgs[0]["exit_code"] == 0
        finally:
            await _shutdown_test(agent_task, reader_holder, server)

    async def test_failing_command(self, tmp_path):
        """Agent reports non-zero exit code."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        agent = Agent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]
            writer = reader_holder["w"]

            await _send_command(writer, "cmd-fail", "exit 7")

            messages = await _collect_messages_until_result(reader, "cmd-fail")
            result = [m for m in messages if m["type"] == "result"][0]
            assert result["exit_code"] == 7
        finally:
            await _shutdown_test(agent_task, reader_holder, server)

    async def test_stderr_output(self, tmp_path):
        """Agent forwards stderr from commands."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        agent = Agent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]
            writer = reader_holder["w"]

            await _send_command(writer, "cmd-err", "echo oops >&2")

            messages = await _collect_messages_until_result(reader, "cmd-err")
            stderr_msgs = [
                m for m in messages
                if m["type"] == "output" and m["stream"] == "stderr"
            ]
            assert len(stderr_msgs) >= 1
            assert "oops" in stderr_msgs[0]["data"]
        finally:
            await _shutdown_test(agent_task, reader_holder, server)


class TestConcurrentCommands:
    async def test_concurrent_execution(self, tmp_path):
        """Multiple commands run concurrently."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        agent = Agent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]
            writer = reader_holder["w"]

            # Send two commands that each take a moment
            await _send_command(writer, "c1", "sleep 0.1 && echo done1")
            await _send_command(writer, "c2", "sleep 0.1 && echo done2")

            # Collect all results
            results = {}
            deadline = asyncio.get_event_loop().time() + 10
            while len(results) < 2:
                remaining = deadline - asyncio.get_event_loop().time()
                line = await asyncio.wait_for(reader.readline(), timeout=remaining)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("type") == "result":
                    results[msg["id"]] = msg

            assert "c1" in results
            assert "c2" in results
            assert results["c1"]["exit_code"] == 0
            assert results["c2"]["exit_code"] == 0
        finally:
            await _shutdown_test(agent_task, reader_holder, server)

    async def test_max_concurrent_commands(self, tmp_path):
        """Semaphore limits concurrent command execution."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        agent = Agent(
            socket_path=path,
            heartbeat_interval=60,
            max_concurrent_commands=1,
        )
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]
            writer = reader_holder["w"]

            # Send two commands; with max_concurrent_commands=1, the second
            # should only start after the first finishes
            await _send_command(writer, "s1", "echo first")
            await _send_command(writer, "s2", "echo second")

            # Both should eventually complete
            results = {}
            deadline = asyncio.get_event_loop().time() + 10
            while len(results) < 2:
                remaining = deadline - asyncio.get_event_loop().time()
                line = await asyncio.wait_for(reader.readline(), timeout=remaining)
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("type") == "result":
                    results[msg["id"]] = msg

            assert results["s1"]["exit_code"] == 0
            assert results["s2"]["exit_code"] == 0
        finally:
            await _shutdown_test(agent_task, reader_holder, server)


class TestWriteSerialization:
    async def test_no_interleaved_writes(self, tmp_path):
        """Concurrent output messages don't produce interleaved JSON lines."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        agent = Agent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]
            writer = reader_holder["w"]

            # Run two commands that produce lots of output concurrently
            await _send_command(
                writer, "w1", "for i in $(seq 1 50); do echo w1-$i; done"
            )
            await _send_command(
                writer, "w2", "for i in $(seq 1 50); do echo w2-$i; done"
            )

            results = set()
            deadline = asyncio.get_event_loop().time() + 10
            while len(results) < 2:
                remaining = deadline - asyncio.get_event_loop().time()
                line = await asyncio.wait_for(reader.readline(), timeout=remaining)
                if not line:
                    break
                # Every line must be valid JSON (no interleaving)
                msg = json.loads(line)
                if msg.get("type") == "result":
                    results.add(msg["id"])

            assert results == {"w1", "w2"}
        finally:
            await _shutdown_test(agent_task, reader_holder, server)


class TestGracefulShutdown:
    async def test_orchestrator_closes_socket(self, tmp_path):
        """Agent exits cleanly when the orchestrator closes the connection."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        on_disconnect_called = asyncio.Event()

        class TestAgent(Agent):
            async def on_disconnect(self):
                on_disconnect_called.set()

        agent = TestAgent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        await asyncio.wait_for(connected.wait(), timeout=5)

        # Close the connection from the server side
        writer = reader_holder["w"]
        writer.close()
        await writer.wait_closed()

        # Agent should exit cleanly
        await asyncio.wait_for(agent_task, timeout=5)
        assert on_disconnect_called.is_set()

        server.close()
        await server.wait_closed()

    async def test_on_connect_called(self, tmp_path):
        """on_connect hook is called when agent connects."""
        path = _socket_path(tmp_path)
        connected_to_server = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected_to_server.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        on_connect_called = asyncio.Event()

        class TestAgent(Agent):
            async def on_connect(self):
                on_connect_called.set()

        agent = TestAgent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected_to_server.wait(), timeout=5)
            assert on_connect_called.is_set()
        finally:
            await _shutdown_test(agent_task, reader_holder, server)

    async def test_send_done(self, tmp_path):
        """Agent can send a done signal explicitly."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        class TestAgent(Agent):
            async def on_connect(self):
                await self.send_done()

        agent = TestAgent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]

            # Should receive the done message.  A ready is always sent
            # after on_connect returns, and a heartbeat may arrive first.
            for _ in range(3):
                msg = await _read_message(reader)
                if msg["type"] == "done":
                    break
                assert msg["type"] in ("ready", "heartbeat")
            else:
                raise AssertionError("Never received a done message")
        finally:
            await _shutdown_test(agent_task, reader_holder, server)

    async def test_send_heartbeat_manual(self, tmp_path):
        """send_heartbeat() works when auto_heartbeat=False."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        class TestAgent(Agent):
            async def on_connect(self):
                await self.send_heartbeat()

        agent = TestAgent(
            socket_path=path,
            heartbeat_interval=60,
            auto_heartbeat=False,
        )
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]

            # on_connect sends a manual heartbeat, then the framework sends
            # ready.  Both should arrive in order.
            msg = await _read_message(reader)
            assert msg["type"] == "heartbeat"
            msg2 = await _read_message(reader)
            assert msg2["type"] == "ready"
        finally:
            await _shutdown_test(agent_task, reader_holder, server)


class TestCustomAgent:
    async def test_override_on_command(self, tmp_path):
        """Subclass can override on_command for custom handling."""
        path = _socket_path(tmp_path)
        connected = asyncio.Event()
        reader_holder = {}

        async def on_connect(reader, writer):
            reader_holder["r"] = reader
            reader_holder["w"] = writer
            connected.set()

        server = await asyncio.start_unix_server(on_connect, path=path)
        os.chmod(path, 0o777)

        from drover_executor import protocol

        class CustomAgent(Agent):
            async def on_command(self, cmd_id, exec_str):
                # Custom: just echo back the command string as output
                await self._send(
                    protocol.encode_output(cmd_id, "stdout", f"custom: {exec_str}")
                )
                await self._send(protocol.encode_result(cmd_id, 0))

        agent = CustomAgent(socket_path=path, heartbeat_interval=60)
        agent_task = asyncio.create_task(agent.run())

        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            reader = reader_holder["r"]
            writer = reader_holder["w"]

            await _send_command(writer, "custom-1", "anything")

            messages = await _collect_messages_until_result(reader, "custom-1")
            output_msgs = [m for m in messages if m["type"] == "output"]
            assert len(output_msgs) == 1
            assert output_msgs[0]["data"] == "custom: anything"
        finally:
            await _shutdown_test(agent_task, reader_holder, server)
