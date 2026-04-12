"""WebSocket endpoints for real-time streaming.

Endpoints:
  WS /containers/{container_id}/exec/stream
    Bidirectional: client sends commands, server streams all output tagged with
    command_id.  Multiple commands may be in flight simultaneously.

  WS /containers/{container_id}/logs/stream
    Server streams Docker container logs until the container stops.
    Optional ?tail=N query parameter (default "all").

Authentication:
  HTTP middleware does not apply to WebSocket connections.  Both endpoints
  accept a ?token=<api_key> query parameter and close with code 1008 if the
  token is missing or invalid (when auth is enabled).
"""

import asyncio
import logging

from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from orchestrator.auth import verify_token
from orchestrator.container_manager import ContainerError, ContainerManager
from orchestrator.database import Database
from orchestrator.docker_client import DockerClient, DockerError
from orchestrator.exec_subscription_manager import ExecSubscriptionManager

router = APIRouter(prefix="/containers", tags=["containers-ws"])

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _authenticate_ws(websocket: WebSocket) -> bool:
    """Verify the ?token= query parameter.

    Returns True if authenticated (or auth is disabled).  On failure, closes
    the WebSocket with code 1008 (Policy Violation) and returns False.
    """
    api_key_hash: str | None = websocket.app.state.config.api_key_hash
    token: str | None = websocket.query_params.get("token")
    if not verify_token(token, api_key_hash):
        await websocket.close(code=1008)
        return False
    return True


async def _safe_close(websocket: WebSocket) -> None:
    """Close the WebSocket, ignoring errors if it is already closed."""
    try:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            await websocket.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Exec stream — WS /containers/{container_id}/exec/stream
# ---------------------------------------------------------------------------


async def _recv_commands(
    websocket: WebSocket,
    container_id: str,
    manager: ContainerManager,
) -> None:
    """Read command messages from the client and dispatch them.

    Exits when the client disconnects or sends an invalid message.
    Each valid command results in a "started" frame being sent back.
    """
    while True:
        try:
            msg = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        except Exception:
            logger.exception("Error receiving WS message for container %s", container_id)
            return

        command = msg.get("command") if isinstance(msg, dict) else None
        if not command or not isinstance(command, str):
            await websocket.send_json(
                {"type": "error", "detail": "Message must be {\"command\": \"<shell command>\"}"}
            )
            continue

        try:
            command_id = await manager.exec_command(container_id, command)
        except ContainerError as e:
            await websocket.send_json({"type": "error", "detail": e.detail})
            continue

        await websocket.send_json({"type": "started", "command_id": command_id})
        logger.info(
            "WS exec: dispatched command %s on container %s", command_id, container_id
        )


async def _forward_output(
    websocket: WebSocket,
    queue: asyncio.Queue,
) -> None:
    """Forward output and result messages from the subscription queue to the client."""
    while True:
        msg = await queue.get()
        try:
            await websocket.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            return


@router.websocket("/{container_id}/exec/stream")
async def exec_stream(websocket: WebSocket, container_id: str) -> None:
    """Stream command output for a container.

    Client sends: {"command": "<shell command>"}
    Server sends:
      {"type": "started",  "command_id": "<id>"}
      {"type": "output",   "command_id": "<id>", "stream": "stdout|stderr", "data": "..."}
      {"type": "result",   "command_id": "<id>", "exit_code": <N>}
      {"type": "error",    "detail": "..."}
    """
    await websocket.accept()
    if not await _authenticate_ws(websocket):
        return

    db: Database = websocket.app.state.db
    exec_subs: ExecSubscriptionManager = websocket.app.state.exec_subs
    manager: ContainerManager = websocket.app.state.container_manager

    row = await db.fetchone(
        "SELECT id, status FROM containers WHERE id = ?", (container_id,)
    )
    if not row:
        await websocket.send_json(
            {"type": "error", "detail": f"Container '{container_id}' not found"}
        )
        await _safe_close(websocket)
        return
    if row["status"] != "running":
        await websocket.send_json(
            {
                "type": "error",
                "detail": f"Container '{container_id}' is not running (status: {row['status']})",
            }
        )
        await _safe_close(websocket)
        return

    queue: asyncio.Queue = asyncio.Queue()
    exec_subs.register(container_id, queue)
    logger.info("WS exec stream connected: container=%s", container_id)

    recv_task = asyncio.create_task(
        _recv_commands(websocket, container_id, manager)
    )
    fwd_task = asyncio.create_task(_forward_output(websocket, queue))

    try:
        _done, pending = await asyncio.wait(
            [recv_task, fwd_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        exec_subs.unregister(container_id, queue)
        await _safe_close(websocket)
        logger.info("WS exec stream closed: container=%s", container_id)


# ---------------------------------------------------------------------------
# Log stream — WS /containers/{container_id}/logs/stream
# ---------------------------------------------------------------------------


@router.websocket("/{container_id}/logs/stream")
async def logs_stream(websocket: WebSocket, container_id: str) -> None:
    """Stream Docker container logs in real-time.

    Optional ?tail=N query parameter (default "all").
    Server sends:
      {"type": "log",   "stream": "stdout|stderr", "data": "..."}
      {"type": "error", "detail": "..."}
    WS closes when the container stops (Docker closes the log stream).
    """
    await websocket.accept()
    if not await _authenticate_ws(websocket):
        return

    db: Database = websocket.app.state.db
    docker: DockerClient = websocket.app.state.docker

    row = await db.fetchone(
        "SELECT docker_id FROM containers WHERE id = ?", (container_id,)
    )
    if not row:
        await websocket.send_json(
            {"type": "error", "detail": f"Container '{container_id}' not found"}
        )
        await _safe_close(websocket)
        return

    tail = websocket.query_params.get("tail", "all")
    docker_id: str = row["docker_id"]

    logger.info(
        "WS log stream connected: container=%s docker_id=%s tail=%s",
        container_id,
        docker_id,
        tail,
    )

    try:
        async for stream, data in docker.stream_container_logs(docker_id, tail=tail):
            await websocket.send_json({"type": "log", "stream": stream, "data": data})
    except WebSocketDisconnect:
        logger.info("WS log stream client disconnected: container=%s", container_id)
    except DockerError as e:
        try:
            await websocket.send_json({"type": "error", "detail": e.message})
        except Exception:
            pass
    except Exception:
        logger.exception("WS log stream error: container=%s", container_id)
    finally:
        await _safe_close(websocket)
        logger.info("WS log stream closed: container=%s", container_id)
