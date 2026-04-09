"""Subprocess execution with async output streaming."""

import asyncio
import logging
import os
import signal as _signal
import subprocess
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

SendFn = Callable[[str, str], Coroutine[Any, Any, None]]


async def run_command(
    exec_str: str,
    send_fn: SendFn,
    chunk_size: int = 8192,
) -> int:
    """Execute a shell command and stream its output.

    Runs ``exec_str`` via ``asyncio.create_subprocess_shell`` with stdout and
    stderr piped.  Each chunk read from the pipes is forwarded immediately via
    ``send_fn(stream, data)`` where *stream* is ``"stdout"`` or ``"stderr"``.

    Returns the process exit code.

    On asyncio cancellation the entire process group is killed to prevent
    orphaned child processes (e.g. a shell's children surviving the shell).
    """
    proc = await asyncio.create_subprocess_shell(
        exec_str,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async def _stream(pipe: asyncio.StreamReader, name: str) -> None:
            while True:
                chunk = await pipe.read(chunk_size)
                if not chunk:
                    break
                await send_fn(name, chunk.decode(errors="replace"))

        await asyncio.gather(
            _stream(proc.stdout, "stdout"),
            _stream(proc.stderr, "stderr"),
        )
        await proc.wait()
        return proc.returncode
    except asyncio.CancelledError:
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        await proc.wait()
        raise
