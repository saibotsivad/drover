"""Captures container stdout/stderr to disk for the full container lifetime.

Owned by the FastAPI ``lifespan``, this manager opens a Docker follow
stream per running container, parses the multiplex frame format, and
writes one ``{log, stream, time}`` JSON object per parsed frame to a
size-rotated file under ``DROVER_LOG_DIR/{container_id}/``.

The on-disk format matches Docker's ``json-file`` driver line format so
log shippers (Promtail, Vector, Fluent Bit) recognize it without
Drover-specific configuration. See ``docs/observability.md`` for the
operator-facing reference and the ADR for the one-JSON-per-chunk
deviation from json-file's line-splitting behavior.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from orchestrator.config import Config
from orchestrator.errors import ContainerError

logger = logging.getLogger(__name__)


_LOG_FILE_RE = re.compile(r"^(\d+)\.log$")
_CURSOR_FILENAME = ".cursor"


# ---------------------------------------------------------------------------
# Multiplex parser
# ---------------------------------------------------------------------------


async def parse_multiplex_stream(
    byte_stream: AsyncIterator[bytes],
) -> AsyncIterator[tuple[str, bytes]]:
    """Yield ``(stream_name, payload_bytes)`` tuples from a Docker mux stream.

    Docker frames look like ``[type:1][0:1][0:1][0:1][len:4 BE][payload]``
    where ``type`` is 1 for stdout and 2 for stderr.  ``payload`` may
    include a leading ``RFC3339Nano `` timestamp when the caller asked
    for ``timestamps=1``; we leave that prefix in place and let the
    caller's record assembler split it off.

    Frames may straddle chunk boundaries arbitrarily — this parser
    buffers until a complete header + payload is available.  A truncated
    final header (less than 8 bytes left at end-of-stream) is treated as
    a graceful EOF: we exit without raising.
    """
    buf = bytearray()
    async for chunk in byte_stream:
        buf.extend(chunk)
        while True:
            if len(buf) < 8:
                break
            stream_type = buf[0]
            payload_len = int.from_bytes(buf[4:8], "big")
            if len(buf) < 8 + payload_len:
                break
            payload = bytes(buf[8 : 8 + payload_len])
            del buf[: 8 + payload_len]
            if stream_type == 1:
                name = "stdout"
            elif stream_type == 2:
                name = "stderr"
            else:
                # Type 0 is stdin and type 3+ is undefined; in either case
                # treat the bytes as stdout so the payload isn't lost.
                name = "stdout"
            yield name, payload


_TS_RE = re.compile(
    rb"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) "
)


def split_timestamp(payload: bytes) -> tuple[str | None, bytes]:
    """Strip Docker's leading ``RFC3339Nano `` prefix from a payload.

    Returns ``(ts_string, remainder)``.  Returns ``(None, payload)`` if
    the prefix is missing or malformed.
    """
    m = _TS_RE.match(payload)
    if not m:
        return None, payload
    ts = m.group(1).decode("ascii")
    return ts, payload[m.end() :]


def _now_iso_z() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_cursor_to_since(ts: str | None) -> int | None:
    """Convert a stored cursor timestamp to Docker's ``since=`` integer.

    Docker's logs API accepts a UNIX timestamp in seconds; we lose
    sub-second precision and accept up to one second of duplicates on
    resume.  Returns ``None`` for a missing or unparseable cursor (the
    caller should request the full history in that case).
    """
    if ts is None:
        return None
    try:
        # RFC3339Nano: trim to microseconds Python can parse, allow Z.
        s = ts.replace("Z", "+00:00")
        # fromisoformat in 3.11+ handles fractional seconds up to 6 digits;
        # truncate any extra nanos before parsing.
        if "." in s:
            head, _, tail = s.partition(".")
            # tail is e.g. "123456789+00:00"; split off the timezone first
            digits = ""
            i = 0
            while i < len(tail) and tail[i].isdigit():
                digits += tail[i]
                i += 1
            digits = digits[:6].ljust(6, "0")
            s = f"{head}.{digits}{tail[i:]}"
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except (ValueError, OverflowError):
        logger.warning("Could not parse cursor timestamp %r", ts)
        return None


# ---------------------------------------------------------------------------
# Per-container capture state
# ---------------------------------------------------------------------------


@dataclass
class _Capture:
    container_id: str
    docker_id: str
    directory: Path
    current_index: int
    current_path: Path
    current_size: int
    last_ts: str | None = None
    task: asyncio.Task | None = None
    file_handle: object | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


# Streamer signature.  We accept a callable so tests can plug in a
# canned byte stream without spinning up a fake DockerClient.
Streamer = Callable[..., AsyncIterator[bytes]]


class LogCaptureManager:
    """Owns the per-container Docker follow streams and disk writers."""

    def __init__(self, config: Config, docker, streamer: Streamer | None = None) -> None:
        self._config = config
        self._docker = docker
        # Allow tests to inject a streamer that returns canned bytes; the
        # production path uses ``docker.stream_container_logs``.
        self._streamer: Streamer = streamer or (
            docker.stream_container_logs if docker is not None else None
        )
        self._captures: dict[str, _Capture] = {}
        self._disk_disabled: bool = False
        self._lock = asyncio.Lock()

    # -- public API --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._config.log_dir is not None

    def container_dir(self, container_id: str) -> Path:
        """Return the on-disk capture directory for *container_id*.

        Raises ``LoggingNotEnabled`` semantics are not enforced here; the
        REST layer checks ``enabled`` before calling this.
        """
        assert self._config.log_dir is not None
        return Path(self._config.log_dir) / container_id

    def read_cursor(self, container_id: str) -> str | None:
        """Return the last persisted timestamp for *container_id*.

        Returns ``None`` when retention is disabled, the directory does
        not exist, or the cursor file is missing/empty.
        """
        if self._config.log_dir is None:
            return None
        path = self.container_dir(container_id) / _CURSOR_FILENAME
        try:
            value = path.read_text(encoding="utf-8").strip()
            return value or None
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("Could not read cursor for %s", container_id, exc_info=True)
            return None

    def list_files(self, container_id: str) -> list[str]:
        """Return the captured filenames for *container_id*, ordered.

        Raises ``LoggingNotEnabled`` when ``DROVER_LOG_DIR`` is unset.
        Returns ``[]`` when the directory does not exist (covers both
        the never-captured and post-``discard`` cases).
        """
        if self._config.log_dir is None:
            raise LoggingNotEnabled()
        directory = self.container_dir(container_id)
        if not directory.is_dir():
            return []
        files = []
        try:
            for entry in directory.iterdir():
                m = _LOG_FILE_RE.match(entry.name)
                if m and entry.is_file():
                    files.append((int(m.group(1)), entry.name))
        except OSError:
            logger.warning(
                "Could not list capture directory for %s", container_id, exc_info=True
            )
            return []
        files.sort(key=lambda x: x[0])
        return [name for _, name in files]

    def file_path(self, container_id: str, filename: str) -> Path:
        """Return the absolute path to a captured file, validating *filename*.

        Raises ``LoggingNotEnabled`` if retention is off, and
        ``LogFileNotFound`` if the filename does not match the
        ``^\\d+\\.log$`` schema or the file does not exist.  The schema
        check is the path-traversal guard.
        """
        if self._config.log_dir is None:
            raise LoggingNotEnabled()
        if not _LOG_FILE_RE.match(filename):
            raise LogFileNotFound(filename)
        path = self.container_dir(container_id) / filename
        if not path.is_file():
            raise LogFileNotFound(filename)
        return path

    async def start(
        self,
        container_id: str,
        docker_id: str,
        since: str | None = None,
    ) -> None:
        """Begin (or restart) capturing logs for *container_id*.

        No-op when ``DROVER_LOG_DIR`` is unset, when global disk-write
        has been disabled (after a persistent OSError), or when a
        capture is already running for the container.
        """
        if self._config.log_dir is None:
            return
        if self._disk_disabled:
            return
        async with self._lock:
            if container_id in self._captures:
                return
            directory = self.container_dir(container_id)
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=0o755)
            except OSError as exc:
                self._handle_oserror(container_id, exc)
                return

            current_index, current_path, current_size = self._pick_initial_file(
                directory
            )
            try:
                file_handle = open(current_path, "ab")
            except OSError as exc:
                self._handle_oserror(container_id, exc)
                return

            capture = _Capture(
                container_id=container_id,
                docker_id=docker_id,
                directory=directory,
                current_index=current_index,
                current_path=current_path,
                current_size=current_size,
                file_handle=file_handle,
            )
            capture.task = asyncio.create_task(
                self._run_capture(capture, since),
                name=f"log-capture:{container_id}",
            )
            self._captures[container_id] = capture
        logger.info(
            "Log capture started for %s (file=%s, since=%s)",
            container_id,
            current_path.name,
            since,
        )

    async def stop(self, container_id: str) -> None:
        """Stop the capture for *container_id* and persist the cursor.

        Idempotent: silently returns when no capture is active.  Always
        runs to completion of the underlying task before returning so
        ``.cursor`` is on disk by the time the caller proceeds.
        """
        capture = self._captures.pop(container_id, None)
        if capture is None:
            return
        await self._terminate_capture(capture)

    async def discard(self, container_id: str) -> None:
        """Stop the capture (if any) and remove the on-disk directory.

        No-op when ``DROVER_LOG_DIR`` is unset.  Idempotent.
        """
        if self._config.log_dir is None:
            return
        await self.stop(container_id)
        directory = self.container_dir(container_id)
        shutil.rmtree(directory, ignore_errors=True)

    async def shutdown(self) -> None:
        """Stop all captures.  Called from FastAPI lifespan teardown."""
        captures = list(self._captures.values())
        self._captures.clear()
        # Run terminations concurrently so a slow Docker close doesn't
        # serialize the shutdown of unrelated containers.
        await asyncio.gather(
            *(self._terminate_capture(c) for c in captures),
            return_exceptions=True,
        )

    # -- internal: capture task -------------------------------------------

    async def _run_capture(self, capture: _Capture, since: str | None) -> None:
        """Read frames from Docker and append records to the rotating file."""
        since_param = _parse_cursor_to_since(since)
        try:
            byte_stream = self._streamer(
                capture.docker_id, since=since_param, follow=True
            )
            async for stream_name, payload in parse_multiplex_stream(byte_stream):
                ts, body = split_timestamp(payload)
                if ts is None:
                    # Missing/malformed prefix — fall back to the
                    # orchestrator's wall clock.  One DEBUG line per
                    # affected frame so the condition is diagnosable.
                    ts = _now_iso_z()
                    logger.debug(
                        "Log frame for %s missing RFC3339Nano prefix; using wall clock",
                        capture.container_id,
                    )
                if not body:
                    capture.last_ts = ts
                    continue
                self._write_record(capture, stream_name, body, ts)
                if self._disk_disabled:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Log capture for %s failed; writer task exiting",
                capture.container_id,
            )
        finally:
            self._flush_and_close(capture)
            self._persist_cursor(capture)

    # -- internal: file ops ------------------------------------------------

    def _pick_initial_file(self, directory: Path) -> tuple[int, Path, int]:
        """Pick the highest existing ``N.log`` (or 0.log if none).

        If that file already exceeds the rotation threshold, advance to
        ``{N+1}.log`` so the first write opens a fresh file.
        """
        max_index = -1
        for entry in directory.iterdir() if directory.is_dir() else []:
            m = _LOG_FILE_RE.match(entry.name)
            if m and entry.is_file():
                idx = int(m.group(1))
                if idx > max_index:
                    max_index = idx
        if max_index < 0:
            return 0, directory / "0.log", 0
        path = directory / f"{max_index}.log"
        size = path.stat().st_size if path.exists() else 0
        if size >= self._config.log_max_file_bytes:
            max_index += 1
            path = directory / f"{max_index}.log"
            size = 0
        return max_index, path, size

    def _write_record(
        self,
        capture: _Capture,
        stream: str,
        body: bytes,
        ts: str,
    ) -> None:
        if self._disk_disabled:
            return
        record = {
            "log": body.decode("utf-8", "replace"),
            "stream": stream,
            "time": ts,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        # Rotate before writing if this record would push the file over
        # the threshold.  The threshold is a ceiling, not a target: we
        # never split a record across files.
        if (
            capture.current_size + len(encoded) > self._config.log_max_file_bytes
            and capture.current_size > 0
        ):
            self._rotate(capture)
        try:
            assert capture.file_handle is not None
            capture.file_handle.write(encoded)  # type: ignore[union-attr]
            capture.file_handle.flush()  # type: ignore[union-attr]
        except OSError as exc:
            self._handle_oserror(capture.container_id, exc)
            return
        capture.current_size += len(encoded)
        capture.last_ts = ts

    def _rotate(self, capture: _Capture) -> None:
        # Close + fsync the current file, persist .cursor, advance index.
        try:
            if capture.file_handle is not None:
                try:
                    os.fsync(capture.file_handle.fileno())  # type: ignore[union-attr]
                except OSError:
                    pass
                capture.file_handle.close()  # type: ignore[union-attr]
        finally:
            capture.file_handle = None
        self._persist_cursor(capture)
        capture.current_index += 1
        capture.current_path = capture.directory / f"{capture.current_index}.log"
        capture.current_size = 0
        try:
            capture.file_handle = open(capture.current_path, "ab")
        except OSError as exc:
            self._handle_oserror(capture.container_id, exc)

    def _flush_and_close(self, capture: _Capture) -> None:
        if capture.file_handle is None:
            return
        try:
            try:
                capture.file_handle.flush()  # type: ignore[union-attr]
            except OSError:
                pass
            capture.file_handle.close()  # type: ignore[union-attr]
        finally:
            capture.file_handle = None

    def _persist_cursor(self, capture: _Capture) -> None:
        if self._disk_disabled or capture.last_ts is None:
            return
        cursor_path = capture.directory / _CURSOR_FILENAME
        tmp_path = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
        try:
            tmp_path.write_text(capture.last_ts, encoding="utf-8")
            os.replace(tmp_path, cursor_path)
        except OSError as exc:
            self._handle_oserror(capture.container_id, exc)

    async def _terminate_capture(self, capture: _Capture) -> None:
        task = capture.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Defensive: the task's finally block should have closed the file
        # and persisted the cursor, but call again so a never-started
        # capture still ends with the file closed.
        self._flush_and_close(capture)
        self._persist_cursor(capture)

    # -- internal: disk-full handling --------------------------------------

    def _handle_oserror(self, container_id: str, exc: OSError) -> None:
        """Disable disk writes globally on the first persistent failure."""
        if self._disk_disabled:
            return
        self._disk_disabled = True
        logger.error(
            "Log capture disabled after disk write failure on container %s "
            "(errno=%s, %s); recovery: free space, restart orchestrator",
            container_id,
            errno.errorcode.get(exc.errno, str(exc.errno)),
            exc,
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


# Imported lazily to avoid a circular dependency with container_manager;
# both modules end up in ``orchestrator`` package which is fine, but we
# keep the exception classes here so log_capture is self-contained.


class LoggingNotEnabled(ContainerError):
    """Raised when log-file endpoints are hit but DROVER_LOG_DIR is unset."""

    def __init__(self) -> None:
        super().__init__(
            409,
            "Container log retention is disabled (DROVER_LOG_DIR is unset)",
        )


class LogFileNotFound(ContainerError):
    """Raised when a requested captured log file does not exist."""

    def __init__(self, filename: str) -> None:
        super().__init__(404, f"Captured log file '{filename}' not found")
