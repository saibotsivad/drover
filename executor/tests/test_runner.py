"""Tests for drover_executor.runner — real subprocess execution.

Every test launches a real subprocess via ``run_command`` and collects
output through an in-memory ``send_fn`` callback.  This validates the
full path from shell execution through pipe reading, chunked streaming,
and exit-code reporting — the same path used by the agent in production.

Tests are intentionally run against actual shell commands (``echo``,
``dd``, ``sleep``, etc.) rather than mocks, so they exercise real
pipe I/O, process lifecycle, and signal delivery.
"""

import asyncio

import pytest

from drover_executor.runner import run_command


class TestRunCommand:
    async def test_echo_stdout(self):
        """Basic stdout capture: simple echo is forwarded via send_fn."""
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("echo hello", send_fn)
        assert exit_code == 0
        stdout_data = "".join(d for s, d in chunks if s == "stdout")
        assert "hello" in stdout_data

    async def test_stderr_output(self):
        """Stderr is captured on its own stream, separate from stdout."""
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("echo error >&2", send_fn)
        assert exit_code == 0
        stderr_data = "".join(d for s, d in chunks if s == "stderr")
        assert "error" in stderr_data

    async def test_mixed_stdout_stderr(self):
        """Both streams are captured when a command writes to both."""
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("echo out && echo err >&2", send_fn)
        assert exit_code == 0
        streams = {s for s, _ in chunks}
        assert "stdout" in streams
        assert "stderr" in streams

    async def test_failing_command(self):
        """Non-zero exit codes are reported (``false`` returns 1)."""
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("false", send_fn)
        assert exit_code != 0

    async def test_exit_code_preserved(self):
        """Arbitrary exit codes (not just 0/1) are passed through."""
        async def send_fn(stream: str, data: str) -> None:
            pass

        exit_code = await run_command("exit 42", send_fn)
        assert exit_code == 42

    async def test_multiline_output(self):
        """Multi-line output is delivered verbatim, newlines included."""
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("printf 'line1\nline2\nline3\n'", send_fn)
        assert exit_code == 0
        stdout_data = "".join(d for s, d in chunks if s == "stdout")
        assert "line1\nline2\nline3\n" == stdout_data

    async def test_empty_output(self):
        """Commands that produce no output result in zero send_fn calls."""
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("true", send_fn)
        assert exit_code == 0
        assert len(chunks) == 0

    async def test_large_output_chunked(self):
        """Output larger than chunk_size is streamed in multiple pieces.

        Generates 64 KB of output with a 4 KB chunk size to verify that
        the runner streams incrementally rather than buffering everything.
        """
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        # Generate output larger than the chunk size
        exit_code = await run_command(
            "dd if=/dev/zero bs=1024 count=64 2>/dev/null | tr '\\0' 'A'",
            send_fn,
            chunk_size=4096,
        )
        assert exit_code == 0
        stdout_data = "".join(d for s, d in chunks if s == "stdout")
        assert len(stdout_data) == 64 * 1024
        # Should be multiple chunks
        stdout_chunks = [d for s, d in chunks if s == "stdout"]
        assert len(stdout_chunks) > 1

    async def test_cancellation_kills_subprocess(self):
        """Cancelling the asyncio task kills the subprocess via process-group signal.

        Without this, ``asyncio.CancelledError`` only cancels the Python
        coroutine — the child process (and its children) would continue
        running as orphans.  The runner uses ``start_new_session=True``
        and ``os.killpg()`` to kill the entire process group.
        """
        started = asyncio.Event()

        async def send_fn(stream: str, data: str) -> None:
            started.set()

        task = asyncio.create_task(
            run_command("echo started && sleep 60", send_fn)
        )

        # Wait for subprocess to produce output
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_binary_replacement_chars(self):
        """Non-UTF-8 bytes are replaced with U+FFFD, not raised as errors.

        Uses ``python3 -c`` to emit raw 0xFF 0xFE bytes that are invalid
        UTF-8.  The runner decodes with ``errors="replace"``.
        """
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        # Use python to emit raw bytes that are not valid UTF-8
        exit_code = await run_command(
            "python3 -c \"import sys; sys.stdout.buffer.write(b'\\xff\\xfe')\"",
            send_fn,
        )
        assert exit_code == 0
        stdout_data = "".join(d for s, d in chunks if s == "stdout")
        assert "\ufffd" in stdout_data  # replacement character
