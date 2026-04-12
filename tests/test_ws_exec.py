"""Tests for WS /containers/{container_id}/exec/stream."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchestrator.auth import hash_api_key
from orchestrator.config import Config
from orchestrator.container_manager import ContainerNotConnected
from orchestrator.exec_subscription_manager import ExecSubscriptionManager
from orchestrator.routers import ws as ws_router

CONTAINER_ID = "cid-test"
CMD_ID = "cmd-test-1"
EXEC_URL = f"/containers/{CONTAINER_ID}/exec/stream"

_RUNNING_ROW = {"id": CONTAINER_ID, "status": "running", "docker_id": "docker-abc"}
_STOPPED_ROW = {"id": CONTAINER_ID, "status": "stopped", "docker_id": "docker-abc"}


def _make_app(
    *,
    api_key_hash: str | None = None,
    container_row=_RUNNING_ROW,
    exec_command_return=CMD_ID,
    exec_command_side_effect=None,
    exec_subs: ExecSubscriptionManager | None = None,
):
    """Build a minimal FastAPI app with only the WS router for testing."""
    if exec_subs is None:
        exec_subs = ExecSubscriptionManager()

    manager = AsyncMock()
    if exec_command_side_effect is not None:
        manager.exec_command.side_effect = exec_command_side_effect
    else:
        manager.exec_command.return_value = exec_command_return

    db = AsyncMock()
    db.fetchone = AsyncMock(return_value=container_row)

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
    app.state.exec_subs = exec_subs
    app.state.container_manager = manager
    app.state.docker = AsyncMock()

    return app, exec_subs, manager


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_auth_disabled_no_token_connects():
    app, _, manager = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect(EXEC_URL) as ws:
            ws.send_json({"command": "echo hi"})
            msg = ws.receive_json()
            assert msg["type"] == "started"
            assert msg["command_id"] == CMD_ID


def test_auth_valid_token_connects():
    key = "secret"
    app, _, _ = _make_app(api_key_hash=hash_api_key(key))
    with TestClient(app) as client:
        with client.websocket_connect(f"{EXEC_URL}?token={key}") as ws:
            ws.send_json({"command": "echo hi"})
            msg = ws.receive_json()
            assert msg["type"] == "started"


def test_auth_missing_token_closes():
    key = "secret"
    app, _, _ = _make_app(api_key_hash=hash_api_key(key))
    with TestClient(app) as client:
        with client.websocket_connect(EXEC_URL) as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1008


def test_auth_wrong_token_closes():
    key = "secret"
    app, _, _ = _make_app(api_key_hash=hash_api_key(key))
    with TestClient(app) as client:
        with client.websocket_connect(f"{EXEC_URL}?token=wrong") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1008


# ---------------------------------------------------------------------------
# Container validation
# ---------------------------------------------------------------------------


def test_container_not_found_sends_error():
    app, _, _ = _make_app(container_row=None)
    with TestClient(app) as client:
        with client.websocket_connect(EXEC_URL) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert CONTAINER_ID in msg["detail"]


def test_container_not_running_sends_error():
    app, _, _ = _make_app(container_row=_STOPPED_ROW)
    with TestClient(app) as client:
        with client.websocket_connect(EXEC_URL) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "not running" in msg["detail"].lower()


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


def test_valid_command_returns_started_frame():
    app, _, manager = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect(EXEC_URL) as ws:
            ws.send_json({"command": "echo hello"})
            msg = ws.receive_json()
            assert msg["type"] == "started"
            assert msg["command_id"] == CMD_ID
    manager.exec_command.assert_awaited_once_with(CONTAINER_ID, "echo hello")


def test_invalid_message_missing_command_returns_error():
    app, _, _ = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect(EXEC_URL) as ws:
            ws.send_json({"not_a_command": "foo"})
            msg = ws.receive_json()
            assert msg["type"] == "error"


def test_exec_command_error_returns_error_frame():
    app, _, manager = _make_app()
    from orchestrator.container_manager import ContainerNotConnected
    manager.exec_command.side_effect = ContainerNotConnected(CONTAINER_ID)
    with TestClient(app) as client:
        with client.websocket_connect(EXEC_URL) as ws:
            ws.send_json({"command": "echo hi"})
            msg = ws.receive_json()
            assert msg["type"] == "error"


def test_multiple_commands_on_same_connection():
    app, _, manager = _make_app()
    manager.exec_command.side_effect = ["cmd-a", "cmd-b"]
    with TestClient(app) as client:
        with client.websocket_connect(EXEC_URL) as ws:
            ws.send_json({"command": "first"})
            msg1 = ws.receive_json()
            assert msg1 == {"type": "started", "command_id": "cmd-a"}

            ws.send_json({"command": "second"})
            msg2 = ws.receive_json()
            assert msg2 == {"type": "started", "command_id": "cmd-b"}


# ---------------------------------------------------------------------------
# Output forwarding
# ---------------------------------------------------------------------------


def test_output_and_result_frames_forwarded():
    """Verify output and result frames arrive via the subscription queue."""
    exec_subs = ExecSubscriptionManager()

    async def exec_side_effect(container_id, command):
        loop = asyncio.get_event_loop()
        loop.call_soon(exec_subs.notify_output, container_id, CMD_ID, "stdout", "hi\n")
        loop.call_soon(exec_subs.notify_result, container_id, CMD_ID, 0)
        return CMD_ID

    app, _, _ = _make_app(exec_subs=exec_subs, exec_command_side_effect=exec_side_effect)
    with TestClient(app) as client:
        with client.websocket_connect(EXEC_URL) as ws:
            ws.send_json({"command": "echo hi"})
            # Collect 3 frames: started + output + result
            msgs = [ws.receive_json() for _ in range(3)]

    types = {m["type"] for m in msgs}
    assert types == {"started", "output", "result"}

    result_msg = next(m for m in msgs if m["type"] == "result")
    assert result_msg["command_id"] == CMD_ID
    assert result_msg["exit_code"] == 0

    output_msg = next(m for m in msgs if m["type"] == "output")
    assert output_msg["command_id"] == CMD_ID
    assert output_msg["stream"] == "stdout"
    assert output_msg["data"] == "hi\n"


def test_multiple_concurrent_ws_clients_both_receive_output():
    """Two WS connections to the same container both receive output."""
    exec_subs = ExecSubscriptionManager()

    call_count = 0

    async def exec_side_effect(container_id, command):
        nonlocal call_count
        call_count += 1
        cmd_id = f"cmd-{call_count}"
        loop = asyncio.get_event_loop()
        loop.call_soon(exec_subs.notify_output, container_id, cmd_id, "stdout", "data\n")
        loop.call_soon(exec_subs.notify_result, container_id, cmd_id, 0)
        return cmd_id

    app, _, _ = _make_app(exec_subs=exec_subs, exec_command_side_effect=exec_side_effect)

    with TestClient(app) as client:
        with client.websocket_connect(EXEC_URL) as ws1:
            with client.websocket_connect(EXEC_URL) as ws2:
                ws1.send_json({"command": "echo from-ws1"})
                # ws1 should receive started + output + result
                msgs1 = [ws1.receive_json() for _ in range(3)]
                # ws2 also registered on same container, should also receive output + result
                msgs2 = [ws2.receive_json() for _ in range(2)]

    types1 = {m["type"] for m in msgs1}
    assert "started" in types1
    assert "output" in types1
    assert "result" in types1

    types2 = {m["type"] for m in msgs2}
    assert "output" in types2
    assert "result" in types2
