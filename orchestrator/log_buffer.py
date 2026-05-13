"""In-memory ring buffer for orchestrator log records.

A ``logging.Handler`` is attached to the root logger and keeps the most
recent records in a bounded ``deque``. The webapp's per-container view
queries this buffer to surface orchestrator-side events that mention a
specific container ID — useful for diagnosing init failures, idle-timeout
reaping, and similar.

The buffer is process-local. Records are lost on orchestrator restart;
that's accepted in v1 (the planned on-disk capture in
``docs/planning/container-log-retention.md`` will eventually cover the
durable case).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


DEFAULT_CAPACITY = 5000


@dataclass(frozen=True)
class LogRecord:
    ts: str           # ISO-8601 UTC
    level: str
    logger: str
    message: str


class RingBufferHandler(logging.Handler):
    """Logging handler that retains the last *capacity* records in memory.

    Records are stored as small immutable dataclasses (not the raw
    ``logging.LogRecord`` objects, which carry references to frames and
    arguments that we don't want to pin).
    """

    _exc_formatter = logging.Formatter()

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        super().__init__()
        self._buf: deque[LogRecord] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                message = (
                    f"{message}\n{self._exc_formatter.formatException(record.exc_info)}"
                )
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
            entry = LogRecord(
                ts=ts,
                level=record.levelname,
                logger=record.name,
                message=message,
            )
            with self._lock:
                self._buf.append(entry)
        except Exception:
            # Never let logging blow up the caller.
            self.handleError(record)

    def snapshot(self) -> list[LogRecord]:
        with self._lock:
            return list(self._buf)

    def filter_contains(self, needle: str, limit: int | None = None) -> list[LogRecord]:
        """Return records whose message contains *needle*.

        When *limit* is set, the most recent *limit* matches are returned
        (still in chronological order).
        """
        if not needle:
            return []
        with self._lock:
            matches = [r for r in self._buf if needle in r.message]
        if limit is not None and len(matches) > limit:
            matches = matches[-limit:]
        return matches
