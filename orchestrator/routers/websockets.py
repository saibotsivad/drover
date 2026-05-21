"""Per-container WebSocket streaming endpoint.

A single endpoint ``/containers/{container_id}/ws`` streams both exec
output (from commands issued via the REST API) and the container's
Docker log stream over one connection.  The ``type`` field on each JSON
message distinguishes the two sources.

Architecture:

* A ``ConnectionManager`` queue is registered for the connection on
  accept.
* The ``SocketManager`` pushes ``output`` and ``status`` messages into
  the queue when the guest agent reports them.
* A background task started by this handler streams Docker logs from
  ``DockerClient.stream_container_logs`` into the same queue.
* The handler loops on ``queue.get()`` and writes to the WebSocket — a
  single consumer for the queue keeps WebSocket sends serialised.

No client-to-server messages are accepted; the connection is one-way
from the server to the client.
"""

import asyncio
import hmac
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from orchestrator.auth import hash_api_key
from orchestrator.connection_manager import ConnectionManager
from orchestrator.docker_client import DockerClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/containers", tags=["websockets"])


def _parse_docker_frames(buf: bytes) -> tuple[list[dict], bytes]:
    """Parse Docker multiplexed stream frames from a byte buffer.

    Each frame is:
        [stream_type: 1 byte][reserved: 3 bytes][size: 4 bytes BE][payload: size bytes]
    where ``stream_type`` is ``1`` for stdout and ``2`` for stderr.

    Returns ``(messages, remaining_bytes)`` — partial frames at the end
    of ``buf`` are returned as the remainder so the caller can prepend
    them to the next chunk.
    """
    messages: list[dict] = []
    pos = 0
    while pos + 8 <= len(buf):
        stream_type = buf[pos]
        size = int.from_bytes(buf[pos + 4 : pos + 8], "big")
        if pos + 8 + size > len(buf):
            break  # incomplete frame; hold for next chunk
        payload = buf[pos + 8 : pos + 8 + size].decode("utf-8", errors="replace")
        stream = "stdout" if stream_type == 1 else "stderr"
        messages.append({"stream": stream, "data": payload})
        pos += 8 + size
    return messages, buf[pos:]


async def _stream_docker_logs(
    docker: DockerClient,
    docker_id: str,
    queue: asyncio.Queue,
    tail: int | None,
) -> None:
    """Stream Docker logs into *queue* until cancelled.

    Parses Docker's multiplexed frame format on the fly and pushes one
    ``{"type": "log", "stream": ..., "data": ...}`` message per frame.
    On Docker error, pushes a ``{"type": "error", ...}`` message and
    exits.
    """
    buf = b""
    try:
        async for chunk in docker.stream_container_logs(
            docker_id, follow=True, tail=tail
        ):
            buf += chunk
            frames, buf = _parse_docker_frames(buf)
            for frame in frames:
                await queue.put({"type": "log", **frame})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Docker log stream failed for %s", docker_id)
        try:
            queue.put_nowait({"type": "error", "message": str(exc)})
        except asyncio.QueueFull:
            pass


@router.websocket("/{container_id}/ws")
async def container_ws(ws: WebSocket, container_id: str) -> None:
    """Stream all exec output and Docker logs for a container.

    Message format (server -> client only, JSON):
      {"type": "output", "command_id": "...", "stream": "stdout|stderr", "data": "..."}
      {"type": "status", "command_id": "...", "status": "complete", "exit_code": N}
      {"type": "log",    "stream": "stdout|stderr", "data": "..."}
      {"type": "error",  "message": "..."}

    Query parameters:
      ``tail`` (int, optional): Docker log lines to replay before live tailing.
    """
    config = ws.app.state.config

    # WebSocket connections bypass ``auth_middleware`` (FastAPI's HTTP
    # middleware doesn't run for the WS upgrade), so authenticate
    # explicitly here before accepting.  Accept the same Bearer header
    # the REST API uses; fall back to ``?token=`` for browser clients
    # that can't set request headers on WebSocket connections.
    if config.api_key_hash is not None:
        auth_header = ws.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            token = ws.query_params.get("token", "")
        if not hmac.compare_digest(hash_api_key(token), config.api_key_hash):
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    db = ws.app.state.db
    docker: DockerClient = ws.app.state.docker
    connection_manager: ConnectionManager = ws.app.state.connection_manager

    row = await db.fetchone(
        "SELECT docker_id FROM containers WHERE id = ?", (container_id,)
    )
    if row is None:
        await ws.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=f"Container '{container_id}' not found",
        )
        return
    docker_id = row["docker_id"]

    tail_param = ws.query_params.get("tail")
    tail: int | None
    if tail_param is None:
        tail = None
    else:
        try:
            tail = int(tail_param)
        except ValueError:
            await ws.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Invalid 'tail' parameter",
            )
            return

    await ws.accept()

    queue = connection_manager.connect(container_id)
    log_task: asyncio.Task | None = None
    if docker_id is not None:
        log_task = asyncio.create_task(
            _stream_docker_logs(docker, docker_id, queue, tail)
        )

    try:
        while True:
            message = await queue.get()
            await ws.send_json(message)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "WebSocket handler error for container %s", container_id
        )
    finally:
        if log_task is not None and not log_task.done():
            log_task.cancel()
            try:
                await log_task
            except (asyncio.CancelledError, Exception):
                pass
        connection_manager.disconnect(container_id, queue)
