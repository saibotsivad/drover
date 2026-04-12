"""Tests for WS /containers/{container_id}/logs/stream."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchestrator.auth import hash_api_key
from orchestrator.config import Config
from orchestrator.docker_client import ContainerNotFoundError
from orchestrator.exec_subscription_manager import ExecSubscriptionManager
from orchestrator.routers import ws as ws_router

CONTAINER_ID = "cid-log-test"
DOCKER_ID = "docker-log-abc"
LOGS_URL = f"/containers/{CONTAINER_ID}/logs/stream"

_CONTAINER_ROW = {"id": CONTAINER_ID, "status": "running", "docker_id": DOCKER_ID}


def _make_app(
    *,
    api_key_hash: str | None = None,
    container_row=_CONTAINER_ROW,
    log_chunks: list[tuple[str, str]] | None = None,
    log_error: Exception | None = None,
):
    """Build a minimal FastAPI app with only the WS router for testing."""
    db = AsyncMock()
    db.fetchone = AsyncMock(return_value=container_row)

    docker = AsyncMock()
    if log_error is not None:
        async def _fail(*args, **kwargs):
            raise log_error
            yield  # make it a generator
        docker.stream_container_logs = _fail
    else:
        chunks = log_chunks or []

        async def _gen(container_id, tail="all"):
            for stream, data in chunks:
                yield stream, data

        docker.stream_container_logs = _gen

    config = Config(
        privileged_image=None,
        db_path=":memory:",
        socket_dir="/tmp",
        docker_sock="/dev/null",
        reaper_interval_seconds=5,
        log_level="DEBUG",
        api_key_hash=api_key_hash,
    )

    app = FastAPI()
    app.include_router(ws_router.router)
    app.state.config = config
    app.state.db = db
    app.state.docker = docker
    app.state.exec_subs = ExecSubscriptionManager()
    app.state.container_manager = AsyncMock()

    return app, docker


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_logs_auth_disabled_connects():
    app, _ = _make_app(log_chunks=[("stdout", "line\n")])
    with TestClient(app) as client:
        with client.websocket_connect(LOGS_URL) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "log"


def test_logs_valid_token_connects():
    key = "secret"
    app, _ = _make_app(api_key_hash=hash_api_key(key), log_chunks=[("stdout", "x\n")])
    with TestClient(app) as client:
        with client.websocket_connect(f"{LOGS_URL}?token={key}") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "log"


def test_logs_missing_token_closes():
    key = "secret"
    app, _ = _make_app(api_key_hash=hash_api_key(key))
    with TestClient(app) as client:
        with client.websocket_connect(LOGS_URL) as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1008


def test_logs_wrong_token_closes():
    key = "secret"
    app, _ = _make_app(api_key_hash=hash_api_key(key))
    with TestClient(app) as client:
        with client.websocket_connect(f"{LOGS_URL}?token=wrong") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1008


# ---------------------------------------------------------------------------
# Container validation
# ---------------------------------------------------------------------------


def test_logs_container_not_found_sends_error():
    app, _ = _make_app(container_row=None)
    with TestClient(app) as client:
        with client.websocket_connect(LOGS_URL) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert CONTAINER_ID in msg["detail"]


# ---------------------------------------------------------------------------
# Log streaming
# ---------------------------------------------------------------------------


def test_logs_streams_stdout_and_stderr():
    chunks = [("stdout", "out line\n"), ("stderr", "err line\n")]
    app, _ = _make_app(log_chunks=chunks)
    with TestClient(app) as client:
        with client.websocket_connect(LOGS_URL) as ws:
            msg1 = ws.receive_json()
            msg2 = ws.receive_json()

    assert msg1 == {"type": "log", "stream": "stdout", "data": "out line\n"}
    assert msg2 == {"type": "log", "stream": "stderr", "data": "err line\n"}


def test_logs_empty_stream_closes_cleanly():
    app, _ = _make_app(log_chunks=[])
    with TestClient(app) as client:
        with client.websocket_connect(LOGS_URL) as ws:
            # No messages; generator exhausted, server closes
            with pytest.raises((WebSocketDisconnect, Exception)):
                ws.receive_json()


def test_logs_docker_error_sends_error_frame():
    app, _ = _make_app(log_error=ContainerNotFoundError(404, "not found"))
    with TestClient(app) as client:
        with client.websocket_connect(LOGS_URL) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"


def test_logs_tail_param_forwarded(tmp_path):
    """?tail=50 query param should be forwarded to stream_container_logs."""
    received_tail = {}

    async def _gen(container_id, tail="all"):
        received_tail["tail"] = tail
        yield "stdout", "line\n"

    app, docker = _make_app(log_chunks=[])
    docker.stream_container_logs = _gen

    with TestClient(app) as client:
        with client.websocket_connect(f"{LOGS_URL}?tail=50") as ws:
            ws.receive_json()  # consume the one log frame

    assert received_tail.get("tail") == "50"


def test_logs_default_tail_is_all():
    received_tail = {}

    async def _gen(container_id, tail="all"):
        received_tail["tail"] = tail
        yield "stdout", "line\n"

    app, docker = _make_app(log_chunks=[])
    docker.stream_container_logs = _gen

    with TestClient(app) as client:
        with client.websocket_connect(LOGS_URL) as ws:
            ws.receive_json()

    assert received_tail.get("tail") == "all"
