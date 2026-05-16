"""Tests for orchestrator.log_capture.

The manager has three failure modes that are easy to get subtly wrong —
frame parsing, file rotation, and disk-full handling — so each gets its
own focused test rather than rolling them into a larger integration
fixture.  Lifecycle wiring (start/stop on container transitions) is
covered in tests/test_container_manager.py.
"""

import asyncio
import errno
import json
import logging
from dataclasses import dataclass

import pytest

from orchestrator.config import Config
from orchestrator.log_capture import (
    LogCaptureManager,
    LogFileNotFound,
    LoggingNotEnabled,
    parse_multiplex_stream,
    split_timestamp,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path, max_bytes: int = 10 * 1024 * 1024) -> Config:
    return Config(
        privileged_image=None,
        data_dir=str(tmp_path),
        socket_dir=str(tmp_path / "sockets"),
        socket_host_dir=str(tmp_path / "sockets"),
        docker_sock="/dev/null",
        reaper_interval_seconds=5,
        init_timeout_seconds=20,
        log_level="DEBUG",
        api_key_hash=None,
        log_dir=str(tmp_path / "logs"),
        log_max_file_bytes=max_bytes,
    )


def _frame(stream_type: int, payload: bytes) -> bytes:
    """Encode a Docker multiplex frame."""
    return bytes([stream_type, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


async def _bytes(chunks: list[bytes]):
    for c in chunks:
        yield c


@dataclass
class _CapturedCall:
    docker_id: str
    since: object
    follow: bool


class _StreamerHarness:
    """Lets a test feed a single byte stream into LogCaptureManager.

    Mimics the ``DockerClient.stream_container_logs`` async-generator
    interface.  Captures the call args so tests can assert ``since=`` is
    plumbed through.
    """

    def __init__(self, chunks: list[bytes], *, hang_after: bool = True):
        self.chunks = chunks
        self.calls: list[_CapturedCall] = []
        # When True, the generator yields all chunks then sleeps forever
        # so the writer task only exits via cancellation — this matches
        # Docker's follow=true behavior and lets stop() trigger the
        # task's cancellation path.
        self.hang_after = hang_after

    def __call__(self, docker_id, *, since=None, follow=True):
        self.calls.append(_CapturedCall(docker_id, since, follow))
        return self._gen()

    async def _gen(self):
        for c in self.chunks:
            yield c
        if self.hang_after:
            # Park forever; the writer task will be cancelled by stop().
            await asyncio.Event().wait()


async def _wait_until(predicate, timeout: float = 2.0):
    """Poll a predicate until True or fail the test on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("predicate never became true")


# ---------------------------------------------------------------------------
# Multiplex parser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parser_interleaved_streams():
    bytes_in = _frame(1, b"out-a") + _frame(2, b"err-b") + _frame(1, b"out-c")
    out: list[tuple[str, bytes]] = []
    async for item in parse_multiplex_stream(_bytes([bytes_in])):
        out.append(item)
    assert out == [
        ("stdout", b"out-a"),
        ("stderr", b"err-b"),
        ("stdout", b"out-c"),
    ]


@pytest.mark.asyncio
async def test_parser_frame_across_chunk_boundaries():
    bytes_in = _frame(1, b"hello, world")
    # Split the frame at three different boundaries to flex the buffering.
    chunks = [bytes_in[:3], bytes_in[3:9], bytes_in[9:]]
    out: list[tuple[str, bytes]] = []
    async for item in parse_multiplex_stream(_bytes(chunks)):
        out.append(item)
    assert out == [("stdout", b"hello, world")]


@pytest.mark.asyncio
async def test_parser_zero_length_payload():
    bytes_in = _frame(1, b"") + _frame(1, b"after")
    out: list[tuple[str, bytes]] = []
    async for item in parse_multiplex_stream(_bytes([bytes_in])):
        out.append(item)
    assert out == [("stdout", b""), ("stdout", b"after")]


@pytest.mark.asyncio
async def test_parser_truncated_header_at_eof():
    bytes_in = _frame(1, b"ok") + b"\x01\x00\x00"  # 3 stray header bytes
    out: list[tuple[str, bytes]] = []
    async for item in parse_multiplex_stream(_bytes([bytes_in])):
        out.append(item)
    # Truncated trailing header is ignored, not raised.
    assert out == [("stdout", b"ok")]


# ---------------------------------------------------------------------------
# Timestamp splitter
# ---------------------------------------------------------------------------


def test_split_timestamp_with_prefix():
    ts, body = split_timestamp(b"2026-05-13T12:34:56.123456789Z hello\n")
    assert ts == "2026-05-13T12:34:56.123456789Z"
    assert body == b"hello\n"


def test_split_timestamp_without_prefix():
    ts, body = split_timestamp(b"no-prefix-here\n")
    assert ts is None
    assert body == b"no-prefix-here\n"


# ---------------------------------------------------------------------------
# Manager: writing, rotation, cursor, disk-full
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writer_emits_one_record_per_frame(tmp_path):
    config = _make_config(tmp_path)
    payload = b"2026-05-13T12:34:56.000000000Z hello world\n"
    stream = _StreamerHarness([_frame(1, payload), _frame(2, b"2026-05-13T12:34:57.000000000Z err\n")])
    manager = LogCaptureManager(config, docker=None, streamer=stream)

    await manager.start("cnt_abc", "docker_xyz")
    await _wait_until(
        lambda: (tmp_path / "logs" / "cnt_abc" / "0.log").exists()
        and (tmp_path / "logs" / "cnt_abc" / "0.log").read_bytes().count(b"\n") >= 2
    )
    await manager.stop("cnt_abc")

    log_path = tmp_path / "logs" / "cnt_abc" / "0.log"
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert lines[0]["log"] == "hello world\n"
    assert lines[0]["stream"] == "stdout"
    assert lines[0]["time"] == "2026-05-13T12:34:56.000000000Z"
    assert lines[1]["log"] == "err\n"
    assert lines[1]["stream"] == "stderr"


@pytest.mark.asyncio
async def test_writer_falls_back_to_wall_clock_when_prefix_missing(tmp_path, caplog):
    config = _make_config(tmp_path)
    payload = b"no-timestamp-here\n"
    stream = _StreamerHarness([_frame(1, payload)])
    manager = LogCaptureManager(config, docker=None, streamer=stream)

    with caplog.at_level(logging.DEBUG, logger="orchestrator.log_capture"):
        await manager.start("cnt_a", "docker_a")
        await _wait_until(
            lambda: (tmp_path / "logs" / "cnt_a" / "0.log").exists()
            and (tmp_path / "logs" / "cnt_a" / "0.log").stat().st_size > 0
        )
        await manager.stop("cnt_a")

    line = json.loads(
        (tmp_path / "logs" / "cnt_a" / "0.log").read_text().splitlines()[0]
    )
    assert line["log"] == "no-timestamp-here\n"
    # Wall-clock fallback emits an ISO-8601 UTC timestamp.
    assert line["time"].endswith("Z")
    # Exactly one DEBUG line per affected frame.
    debug_messages = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "missing RFC3339Nano" in r.getMessage()
    ]
    assert len(debug_messages) == 1


@pytest.mark.asyncio
async def test_rotation_crosses_threshold(tmp_path):
    # Threshold sized so each record fits but two would not.
    config = _make_config(tmp_path, max_bytes=120)
    # Each frame's JSON record is ~83 bytes, so the second write forces rotation.
    payload_a = b"2026-05-13T12:00:00.000000000Z AAAAAAA\n"
    payload_b = b"2026-05-13T12:00:01.000000000Z BBBBBBB\n"
    stream = _StreamerHarness(
        [_frame(1, payload_a), _frame(1, payload_b)],
    )
    manager = LogCaptureManager(config, docker=None, streamer=stream)
    await manager.start("cnt_r", "docker_r")
    await _wait_until(
        lambda: (tmp_path / "logs" / "cnt_r" / "1.log").exists()
        and (tmp_path / "logs" / "cnt_r" / "1.log").stat().st_size > 0
    )
    await manager.stop("cnt_r")

    log_0 = tmp_path / "logs" / "cnt_r" / "0.log"
    log_1 = tmp_path / "logs" / "cnt_r" / "1.log"
    assert log_0.exists() and log_1.exists()
    # 0.log holds exactly one record; both files stay ≤ threshold + last record.
    assert log_0.read_text().count("\n") == 1
    assert log_0.stat().st_size <= config.log_max_file_bytes
    assert log_1.read_text().count("\n") == 1


@pytest.mark.asyncio
async def test_cursor_persists_on_stop_and_is_passed_to_resume(tmp_path):
    config = _make_config(tmp_path)
    payload = b"2026-05-13T12:34:56.123456789Z hello\n"
    stream = _StreamerHarness([_frame(1, payload)])
    manager = LogCaptureManager(config, docker=None, streamer=stream)
    await manager.start("cnt_c", "docker_c")
    await _wait_until(
        lambda: (tmp_path / "logs" / "cnt_c" / "0.log").stat().st_size > 0
        if (tmp_path / "logs" / "cnt_c" / "0.log").exists()
        else False
    )
    await manager.stop("cnt_c")

    cursor_path = tmp_path / "logs" / "cnt_c" / ".cursor"
    assert cursor_path.read_text() == "2026-05-13T12:34:56.123456789Z"

    # Resume: read the cursor and pass it back to start.
    saved = manager.read_cursor("cnt_c")
    assert saved == "2026-05-13T12:34:56.123456789Z"
    stream2 = _StreamerHarness([])
    manager2 = LogCaptureManager(config, docker=None, streamer=stream2)
    await manager2.start("cnt_c", "docker_c", since=saved)
    await _wait_until(lambda: len(stream2.calls) == 1)
    await manager2.stop("cnt_c")
    # Docker since=<unix seconds>; cursor was 2026-05-13T12:34:56Z UTC.
    assert stream2.calls[0].since is not None
    assert isinstance(stream2.calls[0].since, int)


@pytest.mark.asyncio
async def test_resume_reuses_latest_log_file(tmp_path):
    config = _make_config(tmp_path)
    stream = _StreamerHarness(
        [_frame(1, b"2026-05-13T12:00:00.000000000Z first\n")]
    )
    manager = LogCaptureManager(config, docker=None, streamer=stream)
    await manager.start("cnt_x", "docker_x")
    await _wait_until(
        lambda: (tmp_path / "logs" / "cnt_x" / "0.log").exists()
        and (tmp_path / "logs" / "cnt_x" / "0.log").stat().st_size > 0
    )
    await manager.stop("cnt_x")

    first_size = (tmp_path / "logs" / "cnt_x" / "0.log").stat().st_size
    stream2 = _StreamerHarness(
        [_frame(1, b"2026-05-13T12:00:01.000000000Z second\n")]
    )
    manager2 = LogCaptureManager(config, docker=None, streamer=stream2)
    await manager2.start("cnt_x", "docker_x", since=manager.read_cursor("cnt_x"))
    await _wait_until(
        lambda: (tmp_path / "logs" / "cnt_x" / "0.log").stat().st_size > first_size
    )
    await manager2.stop("cnt_x")

    contents = (tmp_path / "logs" / "cnt_x" / "0.log").read_text()
    assert "first" in contents and "second" in contents
    # Both records ended up in 0.log; no rotation happened on resume.
    assert not (tmp_path / "logs" / "cnt_x" / "1.log").exists()


@pytest.mark.asyncio
async def test_disk_full_disables_writes_for_all_containers(tmp_path, caplog, monkeypatch):
    config = _make_config(tmp_path)
    stream_a = _StreamerHarness(
        [_frame(1, b"2026-05-13T12:00:00.000000000Z AAA\n")]
    )
    manager = LogCaptureManager(config, docker=None, streamer=stream_a)

    # Monkeypatch any file's write to raise ENOSPC the first time it's called.
    original_open = open
    handles: list = []

    class _NoSpaceFile:
        def __init__(self, real):
            self._real = real

        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        def flush(self):
            pass

        def close(self):
            self._real.close()

        def fileno(self):
            return self._real.fileno()

    def fake_open(path, mode="r", *args, **kwargs):
        h = original_open(path, mode, *args, **kwargs)
        if str(path).endswith(".log"):
            wrapped = _NoSpaceFile(h)
            handles.append(wrapped)
            return wrapped
        return h

    monkeypatch.setattr("orchestrator.log_capture.open", fake_open, raising=False)

    with caplog.at_level(logging.ERROR, logger="orchestrator.log_capture"):
        await manager.start("cnt_disk1", "docker_d1")
        await _wait_until(lambda: manager._disk_disabled)
        await manager.stop("cnt_disk1")

    error_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "Log capture disabled" in r.getMessage()
    ]
    assert len(error_records) == 1
    assert manager._disk_disabled is True

    # A second container is also a no-op now.
    stream_b = _StreamerHarness(
        [_frame(1, b"2026-05-13T12:00:01.000000000Z BBB\n")]
    )
    manager._streamer = stream_b
    await manager.start("cnt_disk2", "docker_d2")
    # Disabled — no directory should be created.
    assert not (tmp_path / "logs" / "cnt_disk2").exists()


@pytest.mark.asyncio
async def test_discard_removes_directory_and_is_idempotent(tmp_path):
    config = _make_config(tmp_path)
    stream = _StreamerHarness(
        [_frame(1, b"2026-05-13T12:00:00.000000000Z x\n")]
    )
    manager = LogCaptureManager(config, docker=None, streamer=stream)
    await manager.start("cnt_d", "docker_d")
    await _wait_until(
        lambda: (tmp_path / "logs" / "cnt_d" / "0.log").exists()
    )
    await manager.discard("cnt_d")
    assert not (tmp_path / "logs" / "cnt_d").exists()
    # Calling again must not raise.
    await manager.discard("cnt_d")


@pytest.mark.asyncio
async def test_discard_is_noop_when_log_dir_unset(tmp_path):
    config = Config(
        privileged_image=None,
        data_dir=str(tmp_path),
        socket_dir=str(tmp_path / "sockets"),
        socket_host_dir=str(tmp_path / "sockets"),
        docker_sock="/dev/null",
        reaper_interval_seconds=5,
        init_timeout_seconds=20,
        log_level="DEBUG",
        api_key_hash=None,
        log_dir=None,
        log_max_file_bytes=10 * 1024 * 1024,
    )
    manager = LogCaptureManager(config, docker=None)
    # Should not raise even though no directory exists and log_dir is None.
    await manager.discard("anything")


# ---------------------------------------------------------------------------
# File listing endpoints' helpers
# ---------------------------------------------------------------------------


def test_list_files_empty_directory(tmp_path):
    config = _make_config(tmp_path)
    manager = LogCaptureManager(config, docker=None)
    (tmp_path / "logs" / "cnt_e").mkdir(parents=True)
    assert manager.list_files("cnt_e") == []


def test_list_files_sorts_numerically(tmp_path):
    config = _make_config(tmp_path)
    manager = LogCaptureManager(config, docker=None)
    d = tmp_path / "logs" / "cnt_l"
    d.mkdir(parents=True)
    for name in ["0.log", "2.log", "10.log", ".cursor", "garbage.txt"]:
        (d / name).write_text("x")
    assert manager.list_files("cnt_l") == ["0.log", "2.log", "10.log"]


def test_list_files_missing_directory(tmp_path):
    config = _make_config(tmp_path)
    manager = LogCaptureManager(config, docker=None)
    assert manager.list_files("never_existed") == []


def test_list_files_raises_when_disabled(tmp_path):
    config = Config(
        privileged_image=None,
        data_dir=str(tmp_path),
        socket_dir=str(tmp_path / "sockets"),
        socket_host_dir=str(tmp_path / "sockets"),
        docker_sock="/dev/null",
        reaper_interval_seconds=5,
        init_timeout_seconds=20,
        log_level="DEBUG",
        api_key_hash=None,
        log_dir=None,
        log_max_file_bytes=10 * 1024 * 1024,
    )
    manager = LogCaptureManager(config, docker=None)
    with pytest.raises(LoggingNotEnabled):
        manager.list_files("cnt")


def test_file_path_rejects_traversal_and_bad_names(tmp_path):
    config = _make_config(tmp_path)
    manager = LogCaptureManager(config, docker=None)
    d = tmp_path / "logs" / "cnt_p"
    d.mkdir(parents=True)
    (d / "0.log").write_text("hi")

    with pytest.raises(LogFileNotFound):
        manager.file_path("cnt_p", "../etc/passwd")
    with pytest.raises(LogFileNotFound):
        manager.file_path("cnt_p", "foo.log")
    with pytest.raises(LogFileNotFound):
        manager.file_path("cnt_p", "0.txt")
    with pytest.raises(LogFileNotFound):
        manager.file_path("cnt_p", "1.log")  # doesn't exist
    assert manager.file_path("cnt_p", "0.log").name == "0.log"


def test_file_path_raises_when_disabled(tmp_path):
    config = Config(
        privileged_image=None,
        data_dir=str(tmp_path),
        socket_dir=str(tmp_path / "sockets"),
        socket_host_dir=str(tmp_path / "sockets"),
        docker_sock="/dev/null",
        reaper_interval_seconds=5,
        init_timeout_seconds=20,
        log_level="DEBUG",
        api_key_hash=None,
        log_dir=None,
        log_max_file_bytes=10 * 1024 * 1024,
    )
    manager = LogCaptureManager(config, docker=None)
    with pytest.raises(LoggingNotEnabled):
        manager.file_path("cnt", "0.log")
