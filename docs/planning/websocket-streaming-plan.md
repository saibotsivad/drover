# WebSocket Streaming Implementation Plan

> This document outlines the implementation plan for enabling a WebSocket endpoint to stream both command output and container logs, per the decision in `docs/decisions/2026-04-11-websockets-for-streaming.md`.

---

## Overview

The current exec API uses polling (`GET /containers/{id}/execs/{cmd_id}`). We will add a single WebSocket endpoint per container that streams real-time output from two sources:

1. **Command output** - stdout/stderr from commands executed via the guest agent
2. **Container logs** - Docker container logs (stdout/stderr from the container itself)

Both sources flow through a single connection. As soon as the client connects, all new output starts arriving — no subscription step required.

---

## Architecture

### Design Principles

- **Per-container endpoint** - Each container gets its own WebSocket URL to simplify connection management and auth
- **Auto-stream on connect** - All exec output and container logs flow immediately on connect; no subscription messages
- **Single combined stream** - Command output and container logs merge into one WebSocket; `type` field distinguishes them
- **Command correlation** - All exec output messages include `command_id` so clients can correlate with commands they issued
- **No automatic replay** - WebSocket only streams new output; clients use REST API to fetch historical data
- **Backward compatibility** - The existing polling API remains functional
- **Resource cleanup** - Connections must clean up properly when containers stop or clients disconnect

### WebSocket URL Design

```
/containers/{container_id}/ws   - Stream all exec output and container logs for a container
```

This endpoint lives under the existing `/containers` prefix and uses `ws` as the final path segment, matching the pattern of other container sub-resources.

---

## Component Changes

### 1. New WebSocket Router (`orchestrator/routers/websockets.py`)

New router handling WebSocket upgrade requests. Access to `app.state` follows the same `request.app.state.*` pattern used throughout the existing REST routers.

```python
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.requests import Request

router = APIRouter(prefix="/containers", tags=["websockets"])


@router.websocket("/{container_id}/ws")
async def container_ws(ws: WebSocket, container_id: str):
    """Stream all exec output and Docker logs for a container.

    No subscription step — output from all commands and the Docker log
    stream arrives as soon as the connection opens.

    Message format (JSON, server -> client only):
      {"type": "output",  "command_id": "...", "stream": "stdout|stderr", "data": "..."}
      {"type": "status",  "command_id": "...", "status": "pending|running|complete", "exit_code": N}
      {"type": "log",     "stream": "stdout|stderr", "data": "..."}
      {"type": "error",   "message": "..."}
    """
```

The handler:
1. Authenticates the connection (see Auth section)
2. Verifies the container exists via the database
3. Calls `connection_manager.connect(container_id)` to get a `Queue`
4. Starts a background task to stream Docker logs into that queue
5. Loops: `await queue.get()` → `await ws.send_json(message)`
6. On disconnect or error: cancels log task, calls `connection_manager.disconnect(container_id, queue)`

### 2. SocketManager Extension (`orchestrator/socket_manager.py`)

The `SocketManager` handles Unix socket connections to guest agents. It needs a reference to the `ConnectionManager` and two small changes:

```python
class SocketManager:
    def __init__(self, config: Config, db: Database) -> None:
        # Existing fields...
        self._connection_manager: "ConnectionManager | None" = None

    def set_connection_manager(self, cm: "ConnectionManager") -> None:
        """Wire up the ConnectionManager after it is created."""
        self._connection_manager = cm
```

`_handle_output` currently receives `msg` but not `container_id`. The caller `_handle_message` does have `container_id`, so thread it through:

```python
# In _handle_message:
elif msg_type == "output":
    await self._handle_output(container_id, msg)   # add container_id
elif msg_type == "result":
    await self._handle_result(container_id, msg)   # add container_id

# Updated _handle_output:
async def _handle_output(self, container_id: str, msg: dict) -> None:
    command_id = msg.get("id")
    stream = msg.get("stream", "stdout")
    data = msg.get("data", "")
    now = _now_iso()

    await self._db.execute_insert(
        "INSERT INTO command_messages (command_id, stream, data, received_at) "
        "VALUES (?, ?, ?, ?)",
        (command_id, stream, data, now),
    )
    await self._db.execute_insert(
        "UPDATE commands SET status = 'running' "
        "WHERE id = ? AND status = 'pending'",
        (command_id,),
    )

    if self._connection_manager is not None:
        await self._connection_manager.broadcast(container_id, {
            "type": "output",
            "command_id": command_id,
            "stream": stream,
            "data": data,
        })

# Updated _handle_result:
async def _handle_result(self, container_id: str, msg: dict) -> None:
    command_id = msg.get("id")
    exit_code = msg.get("exit_code")

    await self._db.execute_insert(
        "UPDATE commands SET status = 'complete', exit_code = ? WHERE id = ?",
        (exit_code, command_id),
    )

    if self._connection_manager is not None:
        await self._connection_manager.broadcast(container_id, {
            "type": "status",
            "command_id": command_id,
            "status": "complete",
            "exit_code": exit_code,
        })
```

### 3. Docker Client (`orchestrator/docker_client.py`)

`stream_container_logs` is already implemented:

```python
async def stream_container_logs(
    self,
    container_id: str,
    *,
    since: float | int | None = None,
    follow: bool = True,
    tail: int | None = None,
) -> AsyncIterator[bytes]:
```

It yields raw bytes in Docker's multiplexed stream format. The WebSocket handler needs a parser for that frame format. Each frame is:

```
[stream_type: 1 byte][reserved: 3 bytes][payload_size: 4 bytes big-endian][payload: payload_size bytes]
```

`stream_type` values: `1` = stdout, `2` = stderr.

Parser helper (belongs in the websocket router or a shared util):

```python
def _parse_docker_frames(buf: bytes) -> tuple[list[dict], bytes]:
    """Parse Docker multiplexed stream frames from a byte buffer.

    Returns (list of {"stream": ..., "data": ...} dicts, remaining unparsed bytes).
    Partial frames at the end of buf are returned as the remainder so the
    caller can prepend them to the next chunk.
    """
    messages = []
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
```

Usage in the log-streaming background task:

```python
async def _stream_docker_logs(
    docker: DockerClient,
    docker_id: str,
    queue: asyncio.Queue,
    tail: int | None,
) -> None:
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
        await queue.put({"type": "error", "message": str(exc)})
```

### 4. Connection Manager (`orchestrator/connection_manager.py`) (New)

Manages per-container queues. Each connected WebSocket gets its own `asyncio.Queue` so concurrent writes from the log task and the socket manager are safe. No per-command subscription tracking needed.

```python
import asyncio


class ConnectionManager:
    """Manages per-container WebSocket output queues."""

    def __init__(self) -> None:
        # container_id -> set of queues (one per connected WebSocket)
        self._queues: dict[str, set[asyncio.Queue]] = {}

    def connect(self, container_id: str) -> asyncio.Queue:
        """Register a new WebSocket connection; return its dedicated queue."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues.setdefault(container_id, set()).add(queue)
        return queue

    def disconnect(self, container_id: str, queue: asyncio.Queue) -> None:
        """Remove a disconnected WebSocket's queue."""
        queues = self._queues.get(container_id)
        if queues:
            queues.discard(queue)
            if not queues:
                del self._queues[container_id]

    async def broadcast(self, container_id: str, message: dict) -> None:
        """Put a message into every queue registered for container_id.

        Uses put_nowait and drops the message for queues that are full
        (slow consumer) rather than blocking the SocketManager's read loop.
        """
        for queue in list(self._queues.get(container_id, ())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # slow consumer; drop rather than block
```

---

## Data Flow

### Unified Per-Container WebSocket

```
┌─────────────┐     WebSocket      ┌──────────────────────────────────────────┐
│   Client    │◄──────────────────►│  Orchestrator                            │
│             │  JSON messages     │                                          │
└──────┬──────┘                    │  ┌──────────────┐   ┌──────────────────┐ │
       │                           │  │ConnectionMgr │   │  WebSocket       │ │
       │ POST /containers/{id}/    │  │  (queues)    │◄──│  handler         │ │
       │   execs                   │  └──────┬───────┘   └──────────────────┘ │
       │ (issue commands via REST) │         │ broadcast               ▲       │
       │                           │  ┌──────┴───────┐    ┌───────────┴─────┐ │
       │                           │  │SocketManager │    │  Log stream     │ │
       │                           │  │(guest agent) │    │  task           │ │
       │                           │  └──────┬───────┘    └───────────┬─────┘ │
       │                           │         │ Unix socket             │Docker │
       │                           └─────────┼─────────────────────────┼───────┘
       │                                     │                         │
       │                              ┌──────▼──────┐          ┌───────▼─────┐
       │                              │ Guest Agent │          │   Docker    │
       │                              │  (in box)   │          │   Daemon    │
       │                              └─────────────┘          └─────────────┘
       │
       │ GET /containers/{id}/execs/{cmd_id}
       └── (fetch historical output via REST)
```

Two producers write to the same queue; the WebSocket handler is the single consumer. This avoids concurrent WebSocket writes.

---

## Implementation Phases

### Phase 1: Exec Output Streaming

**Goal:** Real-time streaming of command output via per-container WebSocket

**Tasks:**
- [ ] **SocketManager modifications**
  - [ ] Add `_connection_manager` field and `set_connection_manager()` setter
  - [ ] Pass `container_id` to `_handle_output` and `_handle_result`
  - [ ] Broadcast to `ConnectionManager` in `_handle_output` (output message)
  - [ ] Broadcast to `ConnectionManager` in `_handle_result` (status complete message)

- [ ] **ConnectionManager creation**
  - [ ] Create `orchestrator/connection_manager.py`
  - [ ] `connect(container_id) -> Queue`
  - [ ] `disconnect(container_id, queue)`
  - [ ] `broadcast(container_id, message)` with drop-on-full for slow consumers

- [ ] **WebSocket router**
  - [ ] Create `orchestrator/routers/websockets.py`
  - [ ] Implement `GET /containers/{id}/ws` endpoint
  - [ ] Auth check on connect (see Auth section)
  - [ ] Verify container exists; send error + close if not
  - [ ] Register with ConnectionManager; drain queue to WebSocket
  - [ ] Clean up on disconnect

- [ ] **Integration**
  - [ ] Create `ConnectionManager` instance in `app.py` lifespan
  - [ ] Wire to `SocketManager` via `set_connection_manager()`
  - [ ] Add `app.state.connection_manager = connection_manager`
  - [ ] Include WebSocket router in app

**Testing:**
- [ ] Unit tests for `ConnectionManager.broadcast` (drop-on-full behavior)
- [ ] Integration test: connect WebSocket, issue command via REST, receive output
- [ ] Test multiple clients on same container

### Phase 2: Container Log Streaming

**Goal:** Real-time streaming of container stdout/stderr from Docker in the same connection

**Tasks:**
- [ ] **Docker frame parser**
  - [ ] Implement `_parse_docker_frames(buf)` in websocket router or shared util
  - [ ] Handle partial frames across chunk boundaries using a carry buffer

- [ ] **Log streaming background task**
  - [ ] Implement `_stream_docker_logs(docker, docker_id, queue, tail)` coroutine
  - [ ] Start task on WebSocket connect (after accepting connection)
  - [ ] Cancel task on WebSocket disconnect
  - [ ] Look up `docker_id` from DB using `container_id`

- [ ] **Query parameter support**
  - [ ] `tail` (int): historical log lines to replay on connect (default: `None`, no history)

**Testing:**
- [ ] Unit tests for `_parse_docker_frames` (partial frames, multi-stream, empty chunks)
- [ ] Integration test: connect WebSocket, observe both log and exec output messages
- [ ] Test `tail` parameter replays history then follows live

### Phase 3: Documentation & Deployment

**Tasks:**
- [ ] **API documentation**
  - [ ] Document WebSocket message formats
  - [ ] Add sequence diagram: connect → exec → receive output
  - [ ] Python and JavaScript client examples

- [ ] **Deployment docs**
  - [ ] Reverse proxy configuration (nginx, Caddy, Traefik)
  - [ ] WebSocket timeout settings

- [ ] **TODO.md updates**
  - [ ] Mark command streaming complete after Phase 1

---

## API Specification

### WebSocket: Combined Stream

**Endpoint:** `GET /containers/{container_id}/ws`

**Query Parameters:**
- `tail` (int, optional): Number of historical Docker log lines to replay before following live logs. Default: no history.

**Authentication:** `Authorization: Bearer <token>` header during the HTTP upgrade handshake. Note: browsers cannot set custom headers on WebSocket connections — see the Auth section below.

**Connection Flow:**
1. Client opens WebSocket connection
2. Server validates auth and verifies container exists; closes with 1008 on failure
3. Server begins streaming Docker logs (with optional history via `tail`)
4. Server streams exec output for any commands issued via REST API
5. Client issues commands via `POST /containers/{id}/execs` and receives output automatically
6. Client uses `GET /containers/{id}/execs/{command_id}` to fetch historical exec output

**Messages (Server → Client):**

```json
// Exec output chunk
{
  "type": "output",
  "command_id": "cmd_abc123",
  "stream": "stdout",
  "data": "Hello, World!\n"
}

// Exec status: command started executing
{
  "type": "status",
  "command_id": "cmd_abc123",
  "status": "running"
}

// Exec status: command finished
{
  "type": "status",
  "command_id": "cmd_abc123",
  "status": "complete",
  "exit_code": 0
}

// Docker log line
{
  "type": "log",
  "stream": "stdout",
  "data": "2026-01-15T10:30:00.000000000Z Starting service...\n"
}

// Error (connection-ending errors close the socket after sending)
{
  "type": "error",
  "message": "Container not found"
}
```

**No client → server messages.** The connection is a one-way stream from server to client.

**Example Python Client:**

```python
import asyncio
import json
import httpx
import websockets

API_KEY = "secret_key_here"
BASE_URL = "https://api.example.com"
WS_BASE = "wss://api.example.com"
CONTAINER_ID = "cnt_xyz"

auth_headers = {"Authorization": f"Bearer {API_KEY}"}

async def main():
    async with websockets.connect(
        f"{WS_BASE}/containers/{CONTAINER_ID}/ws",
        additional_headers=auth_headers,
    ) as ws:
        # Issue a command via REST while the WebSocket is open
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{BASE_URL}/containers/{CONTAINER_ID}/execs",
                json={"command": "echo hello"},
                headers=auth_headers,
            )
            command_id = resp.json()["command_id"]

        # Receive output — it arrives automatically
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "output":
                print(f"[{msg['command_id']}][{msg['stream']}] {msg['data']}", end="")
            elif msg["type"] == "status" and msg["status"] == "complete":
                print(f"[{msg['command_id']}] exited with code {msg['exit_code']}")
                break
            elif msg["type"] == "log":
                print(f"[docker][{msg['stream']}] {msg['data']}", end="")
            elif msg["type"] == "error":
                print(f"Error: {msg['message']}")
                break

asyncio.run(main())
```

**Example JavaScript Client (browser):**

Browsers cannot set custom headers on WebSocket connections. Pass the token as a query parameter instead and have the server read it from `request.query_params`:

```javascript
const token = 'secret_key_here';
const containerId = 'cnt_xyz';

const ws = new WebSocket(
  `wss://api.example.com/containers/${containerId}/ws?token=${token}`
);

ws.onopen = () => {
  console.log('Connected');

  // Issue a command via REST while the WebSocket is open
  fetch(`/containers/${containerId}/execs`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
    },
    body: JSON.stringify({ command: 'echo hello' }),
  });
};

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  if (msg.type === 'output') {
    console.log(`[${msg.command_id}][${msg.stream}]`, msg.data);
  } else if (msg.type === 'status') {
    console.log(`[${msg.command_id}] status=${msg.status}`, msg.exit_code ?? '');
  } else if (msg.type === 'log') {
    console.log(`[docker][${msg.stream}]`, msg.data);
  } else if (msg.type === 'error') {
    console.error('Server error:', msg.message);
  }
};

ws.onclose = (event) => console.log('Disconnected', event.code);
```

---

## Reverse Proxy Configuration

The `/containers/{id}/ws` path doesn't need a special prefix to match — standard WebSocket proxy configuration on the location that handles all API traffic is sufficient.

### nginx

```nginx
location / {
    proxy_pass http://orchestrator;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 86400;  # 24 hours for long-lived connections
}
```

Or, if only the WebSocket path needs special treatment:

```nginx
location ~ ^/containers/[^/]+/ws$ {
    proxy_pass http://orchestrator;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 86400;
}
```

### Caddy

```caddy
reverse_proxy orchestrator:8000
```

Caddy handles WebSocket upgrades automatically for all proxied traffic.

### Traefik

```yaml
# Docker labels
labels:
  - "traefik.http.routers.drover.rule=PathPrefix(`/`)"
  - "traefik.http.services.drover.loadbalancer.server.port=8000"
  # WebSocket support is automatic in Traefik
```

---

## Error Handling

### Connection Errors

| Scenario | Behavior |
|----------|----------|
| Container not found | Close with 1008 (policy violation) + error message |
| Container not running | Close with 1008 + error message |
| Auth failure | Close with 1008 (see auth flow below) |
| Docker daemon unreachable | Send error message, close connection |

### Runtime Errors

| Scenario | Behavior |
|----------|----------|
| Guest agent disconnects | Queue drains; exec output stops. Docker log stream continues. |
| Docker daemon error during log stream | Send `{"type": "error", ...}` via queue, log task exits |
| Slow consumer (queue full) | Drop message for that connection; do not block SocketManager |
| Client disconnect | Cancel log task, unregister queue from ConnectionManager |

---

## Authentication

The existing middleware (`orchestrator/auth.py`) handles `Authorization: Bearer <token>` for HTTP requests but runs after the WebSocket upgrade completes. For WebSocket connections the auth check must happen explicitly inside the handler before calling `await ws.accept()`:

```python
@router.websocket("/{container_id}/ws")
async def container_ws(ws: WebSocket, container_id: str):
    config = ws.app.state.config
    if config.api_key_hash is not None:
        # Try header first, fall back to ?token= query param for browser clients
        auth_header = ws.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            token = ws.query_params.get("token", "")
        if not hmac.compare_digest(hash_api_key(token), config.api_key_hash):
            await ws.close(code=1008)
            return

    await ws.accept()
    # ... rest of handler
```

---

## Security Considerations

1. **Authentication** - Check `Authorization: Bearer` header or `?token=` query param before accepting the upgrade
2. **Authorization** - Verify the container exists and belongs to the authenticated caller before accepting
3. **Query parameter token exposure** - The `?token=` query param appears in server access logs; document this tradeoff for browser clients
4. **Connection limits** - Consider limiting concurrent WebSocket connections per container
5. **Queue depth** - The 256-message queue cap per connection limits memory exposure from slow consumers

---

## Future Considerations

1. **Bidirectional command input** - Client sends stdin to running commands via the same WebSocket
2. **Interactive shell** - A new endpoint for PTY-based interactive sessions
3. **Log persistence** - Store container logs in SQLite for historical access
4. **Filtered logs** - Query parameters for log filtering (grep, time range)
5. **Dropped message notification** - `{"type": "dropped", "count": N}` to signal slow-consumer drops

---

## Files to Modify/Create

### New Files
- `orchestrator/routers/websockets.py` - WebSocket endpoint + `_parse_docker_frames` + `_stream_docker_logs`
- `orchestrator/connection_manager.py` - Per-container queue registry
- `tests/test_websockets.py` - WebSocket tests

### Modified Files
- `orchestrator/app.py` - Create `ConnectionManager`, wire to `SocketManager`, include WebSocket router
- `orchestrator/socket_manager.py` - Add `set_connection_manager()`, pass `container_id` to `_handle_output`/`_handle_result`, broadcast via `ConnectionManager`
- `TODO.md` - Update status after Phase 1

---

## Open Questions

1. Should a `tail` parameter also replay historical exec output (from `command_messages` table) on connect?
   - Current decision: No. Clients use `GET /containers/{id}/execs/{cmd_id}` for that. The `tail` parameter only applies to Docker logs since the Docker streaming API natively supports it.

2. How to handle very slow WebSocket consumers?
   - Decision: Drop messages when the per-connection queue (maxsize=256) is full. This prevents a slow consumer from blocking the SocketManager's read loop. No `dropped` notification in Phase 1; add it later if needed.

3. Should multiple WebSocket connections to the same container be allowed?
   - Decision: Yes. Each client gets its own queue. Useful for multiple browser tabs or services.
