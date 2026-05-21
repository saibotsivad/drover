"""Tests for the per-container WebSocket streaming endpoint.

Covers:

* ``ConnectionManager`` connect/disconnect/broadcast (drop-on-full)
* Docker multiplexed frame parser (partial/multi-stream/empty inputs)
* WebSocket handler routing (auth, container-not-found, end-to-end
  exec output forwarded via ``SocketManager`` broadcast)
* WebSocket handler Docker-log streaming (forwarded via the background
  task)

The tests stand up a minimal FastAPI app wired with a fake
``DockerClient`` and an in-memory ``Database``-shaped object.  Docker is
not contacted.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.auth import hash_api_key
from orchestrator.config import Config
from orchestrator.connection_manager import ConnectionManager
from orchestrator.routers import websockets as ws_router
from orchestrator.routers.websockets import _parse_docker_frames
from orchestrator.socket_manager import SocketManager


# ---------------------------------------------------------------------------
# ConnectionManager unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_manager_broadcast_to_all_queues():
    cm = ConnectionManager()
    q1 = cm.connect("cnt_a")
    q2 = cm.connect("cnt_a")
    q_other = cm.connect("cnt_b")

    await cm.broadcast("cnt_a", {"type": "output", "data": "hi"})

    assert q1.get_nowait() == {"type": "output", "data": "hi"}
    assert q2.get_nowait() == {"type": "output", "data": "hi"}
    assert q_other.empty()


@pytest.mark.asyncio
async def test_connection_manager_broadcast_no_subscribers():
    cm = ConnectionManager()
    # Should not raise when no queues are registered.
    await cm.broadcast("cnt_missing", {"type": "output"})


@pytest.mark.asyncio
async def test_connection_manager_drops_when_queue_full():
    cm = ConnectionManager()
    q = cm.connect("cnt_a")
    # Fill the queue to its 256-item cap.
    for i in range(256):
        q.put_nowait({"i": i})
    # Broadcast should not raise and should not block.
    await cm.broadcast("cnt_a", {"type": "output", "i": "overflow"})
    assert q.qsize() == 256
    # The dropped message must not be in the queue.
    items = [q.get_nowait() for _ in range(256)]
    assert all(item.get("i") != "overflow" for item in items)


def test_connection_manager_disconnect_removes_queue():
    cm = ConnectionManager()
    q = cm.connect("cnt_a")
    cm.disconnect("cnt_a", q)
    assert "cnt_a" not in cm._queues


def test_connection_manager_disconnect_keeps_others():
    cm = ConnectionManager()
    q1 = cm.connect("cnt_a")
    q2 = cm.connect("cnt_a")
    cm.disconnect("cnt_a", q1)
    assert cm._queues.get("cnt_a") == {q2}


def test_connection_manager_disconnect_unknown_is_noop():
    cm = ConnectionManager()
    q = asyncio.Queue()
    # Should not raise even though q was never registered.
    cm.disconnect("cnt_unknown", q)


# ---------------------------------------------------------------------------
# Docker frame parser unit tests
# ---------------------------------------------------------------------------


def _frame(stream_type: int, payload: bytes) -> bytes:
    header = bytes([stream_type, 0, 0, 0]) + len(payload).to_bytes(4, "big")
    return header + payload


def test_parse_docker_frames_single_stdout():
    buf = _frame(1, b"hello\n")
    messages, remainder = _parse_docker_frames(buf)
    assert messages == [{"stream": "stdout", "data": "hello\n"}]
    assert remainder == b""


def test_parse_docker_frames_stderr_and_stdout():
    buf = _frame(1, b"out\n") + _frame(2, b"err\n")
    messages, remainder = _parse_docker_frames(buf)
    assert messages == [
        {"stream": "stdout", "data": "out\n"},
        {"stream": "stderr", "data": "err\n"},
    ]
    assert remainder == b""


def test_parse_docker_frames_partial_header():
    # Only 4 bytes of header — can't parse anything.
    messages, remainder = _parse_docker_frames(b"\x01\x00\x00\x00")
    assert messages == []
    assert remainder == b"\x01\x00\x00\x00"


def test_parse_docker_frames_partial_payload():
    # Full header, payload truncated to 3 of declared 6 bytes.
    buf = _frame(1, b"hello\n")[:-3]
    messages, remainder = _parse_docker_frames(buf)
    assert messages == []
    assert remainder == buf


def test_parse_docker_frames_complete_then_partial():
    complete = _frame(1, b"done\n")
    partial = _frame(2, b"truncated")[:-4]
    messages, remainder = _parse_docker_frames(complete + partial)
    assert messages == [{"stream": "stdout", "data": "done\n"}]
    assert remainder == partial


def test_parse_docker_frames_empty():
    messages, remainder = _parse_docker_frames(b"")
    assert messages == []
    assert remainder == b""


def test_parse_docker_frames_invalid_utf8_replaced():
    # 0xff is not valid UTF-8; parser uses errors='replace'.
    buf = _frame(1, b"\xff")
    messages, remainder = _parse_docker_frames(buf)
    assert messages[0]["stream"] == "stdout"
    assert "�" in messages[0]["data"]
    assert remainder == b""


# ---------------------------------------------------------------------------
# WebSocket handler tests (TestClient)
# ---------------------------------------------------------------------------


class FakeDb:
    def __init__(self, container_id: str | None, docker_id: str | None):
        self._container_id = container_id
        self._docker_id = docker_id

    async def fetchone(self, sql: str, params: tuple):
        if self._container_id is None:
            return None
        if params and params[0] != self._container_id:
            return None
        return {"docker_id": self._docker_id}


class FakeDocker:
    """Async Docker client stub for streaming tests.

    ``stream_container_logs`` is an async generator that yields each
    chunk handed to the constructor and then waits forever so the
    log-stream background task stays alive.  Tests that exercise the
    handler without log streaming pass ``chunks=None``.
    """

    def __init__(self, chunks: list[bytes] | None = None):
        self._chunks = chunks or []

    async def stream_container_logs(self, docker_id, *, since=None, follow=True, tail=None):
        for chunk in self._chunks:
            yield chunk
            await asyncio.sleep(0)  # let the parser run
        # Hang so the background task doesn't exit before the test reads.
        await asyncio.Event().wait()


def _build_app(
    *,
    api_key_hash: str | None = None,
    container_id: str | None = "cnt_abc",
    docker_id: str | None = "dockerid123",
    docker_chunks: list[bytes] | None = None,
) -> tuple[FastAPI, ConnectionManager]:
    app = FastAPI()
    app.include_router(ws_router.router)

    config = Config(
        privileged_image=None,
        db_path="/tmp/x.db",
        socket_dir="/tmp/s",
        docker_sock="/dev/null",
        reaper_interval_seconds=5,
        init_timeout_seconds=20,
        log_level="DEBUG",
        api_key_hash=api_key_hash,
        log_dir=None,
        log_max_file_bytes=10 * 1024 * 1024,
    )
    cm = ConnectionManager()
    app.state.config = config
    app.state.db = FakeDb(container_id, docker_id)
    app.state.docker = FakeDocker(docker_chunks)
    app.state.connection_manager = cm
    return app, cm


def _broadcast_via_portal(ws_session, cm: ConnectionManager, container_id: str, msg: dict) -> None:
    """Inject a broadcast in the WS handler's event loop.

    ``ConnectionManager`` queues are bound to the WS handler's loop;
    calling ``cm.broadcast`` from the test thread would touch a queue
    owned by a different loop and silently never wake the handler.
    ``WebSocketTestSession.portal`` runs the coroutine on the right
    loop and blocks until it completes.
    """
    ws_session.portal.call(cm.broadcast, container_id, msg)


def test_ws_rejects_missing_container():
    app, _ = _build_app(container_id=None)
    client = TestClient(app)
    # Starlette's TestClient raises WebSocketDisconnect when the server
    # closes before/after accept.
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/containers/cnt_xyz/ws"):
            pass
    assert exc.value.code == 1008


def test_ws_rejects_invalid_auth():
    key = "secret"
    app, _ = _build_app(api_key_hash=hash_api_key(key))
    client = TestClient(app)
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/containers/cnt_abc/ws",
            headers={"Authorization": "Bearer wrong"},
        ):
            pass
    assert exc.value.code == 1008


def test_ws_accepts_valid_bearer_header():
    key = "secret"
    app, cm = _build_app(api_key_hash=hash_api_key(key))
    client = TestClient(app)
    with client.websocket_connect(
        "/containers/cnt_abc/ws",
        headers={"Authorization": f"Bearer {key}"},
    ) as ws:
        _broadcast_via_portal(ws, cm, "cnt_abc", {"type": "output", "data": "hello"})
        msg = ws.receive_json()
    assert msg == {"type": "output", "data": "hello"}


def test_ws_accepts_token_query_param():
    key = "secret"
    app, cm = _build_app(api_key_hash=hash_api_key(key))
    client = TestClient(app)
    with client.websocket_connect(
        f"/containers/cnt_abc/ws?token={key}"
    ) as ws:
        _broadcast_via_portal(
            ws, cm, "cnt_abc", {"type": "log", "stream": "stdout", "data": "x"}
        )
        msg = ws.receive_json()
    assert msg == {"type": "log", "stream": "stdout", "data": "x"}


def test_ws_rejects_invalid_tail():
    app, _ = _build_app()
    client = TestClient(app)
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/containers/cnt_abc/ws?tail=notanumber"):
            pass
    assert exc.value.code == 1008


def test_ws_forwards_docker_log_frames():
    frame = _frame(1, b"line1\n") + _frame(2, b"line2\n")
    app, _ = _build_app(docker_chunks=[frame])
    client = TestClient(app)
    with client.websocket_connect("/containers/cnt_abc/ws") as ws:
        msg1 = ws.receive_json()
        msg2 = ws.receive_json()
    assert msg1 == {"type": "log", "stream": "stdout", "data": "line1\n"}
    assert msg2 == {"type": "log", "stream": "stderr", "data": "line2\n"}


def test_ws_forwards_log_frames_split_across_chunks():
    # Split a single 6-byte payload frame across two chunks so the
    # parser has to carry the partial frame.
    full = _frame(1, b"abcdef")
    chunk_a, chunk_b = full[:5], full[5:]
    app, _ = _build_app(docker_chunks=[chunk_a, chunk_b])
    client = TestClient(app)
    with client.websocket_connect("/containers/cnt_abc/ws") as ws:
        msg = ws.receive_json()
    assert msg == {"type": "log", "stream": "stdout", "data": "abcdef"}


def test_ws_skips_log_stream_when_docker_id_missing():
    # Container exists but has no Docker ID yet (e.g. mid-init).
    app, cm = _build_app(docker_id=None)
    client = TestClient(app)
    with client.websocket_connect("/containers/cnt_abc/ws") as ws:
        # No log task was started, but the connection is open and
        # ready to receive broadcasts.
        _broadcast_via_portal(
            ws, cm, "cnt_abc", {"type": "output", "data": "from-exec"}
        )
        msg = ws.receive_json()
    assert msg == {"type": "output", "data": "from-exec"}


def test_ws_disconnects_cleanly_releases_queue():
    app, cm = _build_app()
    client = TestClient(app)
    with client.websocket_connect("/containers/cnt_abc/ws"):
        assert "cnt_abc" in cm._queues
    # TestClient context manager closes the socket; the handler's
    # finally block must unregister the queue.  TestClient drives the
    # server in a background thread, so give the handler a beat to
    # finish cleanup.
    import time
    for _ in range(50):
        if "cnt_abc" not in cm._queues:
            break
        time.sleep(0.02)
    assert "cnt_abc" not in cm._queues


# ---------------------------------------------------------------------------
# SocketManager <-> ConnectionManager integration
# ---------------------------------------------------------------------------


class _RecordingDb:
    """Minimal Database stub for SocketManager broadcast tests."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute_insert(self, sql, params=()):
        self.calls.append((sql, params))


@pytest.mark.asyncio
async def test_socket_manager_broadcasts_output_to_connection_manager(config):
    sm = SocketManager(config, _RecordingDb())
    cm = ConnectionManager()
    sm.set_connection_manager(cm)
    queue = cm.connect("cnt_abc")

    await sm._handle_output(
        "cnt_abc",
        {"id": "cmd_1", "stream": "stderr", "data": "boom\n"},
    )
    msg = queue.get_nowait()
    assert msg == {
        "type": "output",
        "command_id": "cmd_1",
        "stream": "stderr",
        "data": "boom\n",
    }


@pytest.mark.asyncio
async def test_socket_manager_broadcasts_complete_status(config):
    sm = SocketManager(config, _RecordingDb())
    cm = ConnectionManager()
    sm.set_connection_manager(cm)
    queue = cm.connect("cnt_abc")

    await sm._handle_result("cnt_abc", {"id": "cmd_1", "exit_code": 0})
    msg = queue.get_nowait()
    assert msg == {
        "type": "status",
        "command_id": "cmd_1",
        "status": "complete",
        "exit_code": 0,
    }


@pytest.mark.asyncio
async def test_socket_manager_no_broadcast_without_connection_manager(config):
    """When no ConnectionManager is wired, handlers don't blow up."""
    sm = SocketManager(config, _RecordingDb())
    # Don't call set_connection_manager.
    await sm._handle_output(
        "cnt_abc", {"id": "cmd_1", "stream": "stdout", "data": "x"}
    )
    await sm._handle_result("cnt_abc", {"id": "cmd_1", "exit_code": 0})
