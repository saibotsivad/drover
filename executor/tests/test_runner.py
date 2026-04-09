"""Tests for drover_executor.runner — real subprocess execution."""

import asyncio

import pytest

from drover_executor.runner import run_command


class TestRunCommand:
    async def test_echo_stdout(self):
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("echo hello", send_fn)
        assert exit_code == 0
        stdout_data = "".join(d for s, d in chunks if s == "stdout")
        assert "hello" in stdout_data

    async def test_stderr_output(self):
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("echo error >&2", send_fn)
        assert exit_code == 0
        stderr_data = "".join(d for s, d in chunks if s == "stderr")
        assert "error" in stderr_data

    async def test_mixed_stdout_stderr(self):
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("echo out && echo err >&2", send_fn)
        assert exit_code == 0
        streams = {s for s, _ in chunks}
        assert "stdout" in streams
        assert "stderr" in streams

    async def test_failing_command(self):
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("false", send_fn)
        assert exit_code != 0

    async def test_exit_code_preserved(self):
        async def send_fn(stream: str, data: str) -> None:
            pass

        exit_code = await run_command("exit 42", send_fn)
        assert exit_code == 42

    async def test_multiline_output(self):
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("printf 'line1\nline2\nline3\n'", send_fn)
        assert exit_code == 0
        stdout_data = "".join(d for s, d in chunks if s == "stdout")
        assert "line1\nline2\nline3\n" == stdout_data

    async def test_empty_output(self):
        chunks = []

        async def send_fn(stream: str, data: str) -> None:
            chunks.append((stream, data))

        exit_code = await run_command("true", send_fn)
        assert exit_code == 0
        assert len(chunks) == 0

    async def test_large_output_chunked(self):
        """Verify output is streamed in chunks, not buffered entirely."""
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
        """Cancelling the task should kill the child process."""
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
        """Non-UTF-8 bytes should be replaced, not raise."""
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
