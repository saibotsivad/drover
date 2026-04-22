# WebSocket Streaming Implementation Plan

> This document outlines the implementation plan for enabling WebSocket endpoints to stream both command output and container logs, per the decision in `docs/decisions/2026-04-11-websockets-for-streaming.md`.

---

## Overview

The current exec API uses polling (`GET /containers/{id}/exec/{cmd_id}`). We will add WebSocket endpoints for real-time streaming of:

1. **Command output** - Stream stdout/stderr from commands executed via the guest agent
2. **Container logs** - Stream Docker container logs (stdout/stderr from the container itself)

---

## Architecture

### Design Principles

- **Per-container endpoints** - Each container gets its own WebSocket URLs to simplify connection management and auth
- **Connect before command** - Client connects to command output stream first, then issues commands via REST API
- **No automatic replay** - WebSocket only streams new output; clients use REST API to fetch historical data
- **Command correlation** - All command output messages include `command_id` so clients can correlate with commands they issued
- **Separate streams** - Command output and container logs are separate concerns with different lifecycles
- **Backward compatibility** - The existing polling API remains functional
- **Resource cleanup** - Connections must clean up properly when containers stop or clients disconnect

### WebSocket URL Design

```
/ws/containers/{container_id}/exec   - Stream all command output for a container
/ws/containers/{container_id}/logs   - Stream container logs (Docker stdout/stderr)
```

**Key change:** The command output endpoint is per-container, not per-command. This allows:
1. Client connects WebSocket first
2. Client issues command via `POST /containers/{id}/exec`
3. Client subscribes to receive output for specific commands via WebSocket
4. Client can issue multiple commands and receive all output through one connection

---

## Component Changes

### 1. New WebSocket Router (`orchestrator/routers/websockets.py`)

New router handling WebSocket upgrade requests:

```python
@router.websocket("/ws/containers/{container_id}/exec")
async def exec_stream(ws: WebSocket, container_id: str):
    """Stream all command output for a container in real-time.
    
    Client connects first, then issues commands via REST API. Output for all
    commands is streamed through this single connection.
    
    Message format (JSON):
    - Server -> Client:
      {"type": "output", "command_id": "...", "stream": "stdout|stderr", "data": "..."}
      {"type": "status", "command_id": "...", "status": "pending|running|complete", "exit_code": N}
      {"type": "error", "message": "..."}
    - Client -> Server:
      {"type": "subscribe", "command_id": "..."}  # Subscribe to output for a specific command
      {"type": "cancel", "command_id": "..."}     # Optional: request command cancellation
    """

@router.websocket("/ws/containers/{container_id}/logs")
async def container_logs_stream(ws: WebSocket, container_id: str):
    """Stream container logs from Docker.
    
    Query params:
    - tail: Number of lines to include from history (default: 0 for new logs only)
    - follow: Whether to follow new logs (default: true)
    
    Message format (JSON):
    {"type": "log", "stream": "stdout|stderr", "data": "..."}
    """
```

### 2. SocketManager Extension (`orchestrator/socket_manager.py`)

The `SocketManager` handles Unix socket connections to guest agents. We need to add:

```python
class SocketManager:
    def __init__(...):
        # Existing...
        # container_id -> set of queues (one per WebSocket connection)
        self._container_subscribers: dict[str, set[asyncio.Queue]] = {}
        
    async def subscribe_container(self, container_id: str, queue: asyncio.Queue) -> None:
        """Subscribe a WebSocket consumer to all command output for a container."""
        
    async def unsubscribe_container(self, container_id: str, queue: asyncio.Queue) -> None:
        """Unsubscribe a WebSocket consumer."""
        
    async def _handle_output(self, msg: dict) -> None:
        """Modified to broadcast to all container subscribers with command_id in message."""
        # msg contains "id" (command_id), "stream", "data"
        # Broadcast to all subscribers for this container
```

### 3. Docker Client Extension (`orchestrator/docker_client.py`)

Add streaming log support:

```python
async def stream_container_logs(
    self, container_id: str, *, tail: int = 0, follow: bool = True
) -> AsyncIterator[dict]:
    """Stream container logs from Docker.
    
    Yields: {"stream": "stdout|stderr", "data": "..."}
    """
```

### 4. Connection Manager (`orchestrator/connection_manager.py`) (New)

A new component to manage WebSocket connections and handle:

- Connection authentication/authorization
- Connection lifecycle (cleanup on disconnect)
- Message broadcasting to multiple subscribers
- Backpressure handling (slow consumers)
- Command-specific subscription management

```python
class ConnectionManager:
    """Manages WebSocket connections for streaming."""
    
    def __init__(self):
        # container_id -> set of WebSockets
        self._exec_connections: dict[str, set[WebSocket]] = {}
        # container_id -> set of WebSockets
        self._log_connections: dict[str, set[WebSocket]] = {}
        # Track which commands each connection wants to receive
        self._connection_subscriptions: dict[WebSocket, set[str]] = {}  # ws -> set of command_ids
        
    async def connect_exec(self, container_id: str, ws: WebSocket) -> None:
        """Register a WebSocket for container command output."""
        
    async def subscribe_command(self, ws: WebSocket, command_id: str) -> None:
        """Subscribe a connection to receive output for a specific command."""
        # Note: No replay. Client uses GET /containers/{id}/exec/{cmd_id} for history.
        
    async def disconnect(self, ws: WebSocket) -> None:
        """Clean up a disconnected WebSocket."""
        
    async def broadcast_to_container(self, container_id: str, message: dict) -> None:
        """Broadcast a message to all WebSockets subscribed to a container's exec stream."""
        # Only send to connections that have subscribed to this specific command
```

---

## Data Flow

### Command Output Streaming (Per-Container WebSocket)

```
┌─────────────┐     WebSocket      ┌──────────────┐     Unix Socket      ┌─────────────┐
│   Client    │◄──────────────────►│  Orchestrator│◄────────────────────►│ Guest Agent │
│             │  (new output only) │              │                      │  (in box)   │
└──────┬──────┘                    └──────────────┘                      └─────────────┘
       │                                    │
       │ POST /exec                         │ INSERT INTO command_messages
       │ (issue commands)                   ▼
       │                             ┌──────────────┐
       └────────────────────────────►│   SQLite     │
                                     └──────────────┘
       │                                    ▲
       │ GET /exec/{cmd_id}                 │
       │ (fetch history)                    │
```

1. **Client connects** to `/ws/containers/{id}/exec`
2. **Client issues command** via `POST /containers/{id}/exec` → gets `command_id`
3. **Client subscribes** to that command via WebSocket message: `{"type": "subscribe", "command_id": "..."}`
4. **Server streams** only new output as it arrives from the guest agent
5. **Client fetches history** (if needed) via existing `GET /containers/{id}/exec/{command_id}` endpoint
6. **Client can repeat** steps 2-5 for multiple commands over the same WebSocket

**Key point:** The WebSocket only streams new output. Historical data is fetched via the existing REST API, which already supports pagination and is better suited for retrieving potentially large amounts of data.

### Container Logs Streaming

```
┌─────────────┐     WebSocket      ┌──────────────┐     Docker API       ┌─────────────┐
│   Client    │◄──────────────────►│  Orchestrator│◄────────────────────►│   Docker    │
│             │                    │              │   /containers/{id}   │   Daemon    │
└─────────────┘                    └──────────────┘   /logs?follow=1     └─────────────┘
```

1. Client connects to `/ws/containers/{id}/logs`
2. Orchestrator verifies container exists
3. Orchestrator starts Docker log stream
4. Docker logs are forwarded to WebSocket (not persisted)

---

## Implementation Phases

### Phase 1: Command Output Streaming

**Goal:** Real-time streaming of command output via per-container WebSocket

**Tasks:**
- [ ] **SocketManager modifications**
  - [ ] Add subscriber registry: `container_id -> set of queues`
  - [ ] Add `subscribe_container()` method
  - [ ] Add `unsubscribe_container()` method
  - [ ] Modify `_handle_output()` to broadcast to container subscribers
  - [ ] Include `command_id` in broadcasted messages

- [ ] **ConnectionManager creation**
  - [ ] Create `orchestrator/connection_manager.py`
  - [ ] Manage per-container WebSocket connections
  - [ ] Handle command-specific subscriptions (no replay)
  - [ ] Handle cleanup on disconnect

- [ ] **WebSocket router**
  - [ ] Create `routers/websockets.py`
  - [ ] Implement `/ws/containers/{id}/exec` endpoint (per-container)
  - [ ] Handle subscription messages from client
  - [ ] Handle connection lifecycle

- [ ] **Integration**
  - [ ] Add WebSocket router to main app
  - [ ] Update `TODO.md` to mark command streaming as complete

**Testing:**
- [ ] Unit tests for subscriber management
- [ ] Integration test with mock guest agent
- [ ] Manual test: connect WebSocket, issue command, receive output
- [ ] Test multiple commands over one connection

### Phase 2: Container Logs Streaming

**Goal:** Real-time streaming of container stdout/stderr from Docker

**Tasks:**
- [ ] **Docker client extension**
  - [ ] Add `stream_container_logs()` method using Docker's streaming API
  - [ ] Handle multiplexed Docker stream format

- [ ] **WebSocket endpoint**
  - [ ] Add `/ws/containers/{id}/logs` endpoint
  - [ ] Support `tail` and `follow` query parameters
  - [ ] Handle Docker stream parsing

- [ ] **Connection management**
  - [ ] Handle multiple concurrent log subscribers per container
  - [ ] Clean up Docker streams when last subscriber disconnects

**Testing:**
- [ ] Unit tests for Docker stream parsing
- [ ] Integration test with Docker
- [ ] Test concurrent subscribers

### Phase 3: Documentation & Deployment

**Tasks:**
- [ ] **API documentation**
  - [ ] Document WebSocket message formats
  - [ ] Add sequence diagrams showing connect → exec → subscribe → receive flow
  - [ ] Add examples for JavaScript/Python clients

- [ ] **Deployment docs**
  - [ ] Add reverse proxy configuration (nginx, Caddy, Traefik)
  - [ ] Document WebSocket-specific timeout settings

- [ ] **TODO.md updates**
  - [ ] Remove completed items
  - [ ] Add notes about potential future bidirectional features

---

## API Specification

### WebSocket: Command Output Stream (Per-Container)

**Endpoint:** `GET /ws/containers/{container_id}/exec`

**Authentication:** Same as REST API (API key in header during upgrade)

**Connection Flow:**
1. Client opens WebSocket connection
2. Client issues command via REST API: `POST /containers/{id}/exec`
3. Client receives `command_id` in response
4. Client sends subscription message via WebSocket
5. Server acknowledges subscription
6. Server streams only **new** output as it arrives
7. (Optional) Client fetches historical output via `GET /containers/{id}/exec/{command_id}`

**Messages (Client → Server):**

```json
// Subscribe to a specific command's output
{
  "type": "subscribe",
  "command_id": "cmd_abc123"
}

// Cancel a running command (future)
{
  "type": "cancel",
  "command_id": "cmd_abc123"
}
```

**Messages (Server → Client):**

```json
// Acknowledge subscription
{
  "type": "subscribed",
  "command_id": "cmd_abc123"
}

// Output chunk (only new output, no replay)
{
  "type": "output",
  "command_id": "cmd_abc123",
  "stream": "stdout",
  "data": "Hello, World!\n"
}

// Status update
{
  "type": "status",
  "command_id": "cmd_abc123",
  "status": "running"
}

// Command completion
{
  "type": "status",
  "command_id": "cmd_abc123",
  "status": "complete",
  "exit_code": 0
}

// Error
{
  "type": "error",
  "message": "Container not found"
}
```

**Example JavaScript Client:**

```javascript
const ws = new WebSocket('wss://api.example.com/ws/containers/cnt_xyz/exec', [], {
  headers: { 'X-API-Key': 'secret_key_here' }
});

ws.onopen = () => {
  console.log('Connected to container exec stream');
};

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  
  if (msg.type === 'output') {
    console.log(`[${msg.command_id}][${msg.stream}] ${msg.data}`);
  } else if (msg.type === 'status') {
    console.log(`[${msg.command_id}] Status: ${msg.status}`, msg.exit_code);
  }
};

// Issue a command via REST API
const response = await fetch('/containers/cnt_xyz/exec', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-API-Key': 'secret_key_here' },
  body: JSON.stringify({ command: 'echo hello' })
});
const { command_id } = await response.json();

// Subscribe to receive its output via WebSocket
ws.send(JSON.stringify({ type: 'subscribe', command_id }));

// (Optional) Fetch historical output via REST API
const history = await fetch(`/containers/cnt_xyz/exec/${command_id}`, {
  headers: { 'X-API-Key': 'secret_key_here' }
});
const { messages } = await history.json();
```

### WebSocket: Container Logs Stream

**Endpoint:** `GET /ws/containers/{container_id}/logs?tail=100&follow=true`

**Query Parameters:**
- `tail` (int): Number of historical log lines to include (default: 0)
- `follow` (bool): Whether to follow new logs (default: true)

**Messages (Server → Client):**

```json
// Log line
{
  "type": "log",
  "stream": "stdout",
  "data": "2024-01-15T10:30:00Z Starting service...\n"
}

// Stream end (when follow=false or container stops)
{
  "type": "end"
}

// Error
{
  "type": "error",
  "message": "Container not found"
}
```

---

## Reverse Proxy Configuration

### nginx

```nginx
location /ws/ {
    proxy_pass http://orchestrator;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 86400;  # 24 hours for long-lived connections
}
```

### Caddy

```caddy
@websockets {
    path /ws/*
}
reverse_proxy @websockets orchestrstrator:8000
```

### Traefik

```yaml
# Docker labels
labels:
  - "traefik.http.routers.drover.rule=PathPrefix(`/ws/`)")
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
| Auth failure | Close with 1008 (handled by auth middleware) |

### Runtime Errors

| Scenario | Behavior |
|----------|----------|
| Invalid command_id in subscribe | Send error message, keep connection open |
| Guest agent disconnects | Send error to affected command subscribers, keep connection for new commands |
| Docker daemon error | Send error message, then close |
| Client disconnect | Clean up all subscriptions |

---

## Security Considerations

1. **Authentication** - WebSocket connections must pass the same API key validation as REST endpoints
2. **Authorization** - A client can only stream from containers they have access to (verified on connect)
3. **Rate limiting** - Consider rate limits for WebSocket message volume
4. **Connection limits** - Limit concurrent WebSocket connections per container

---

## Future Considerations

This design leaves room for future enhancements without breaking changes:

1. **Bidirectional command input** - Client can send stdin to running commands via the same WebSocket
2. **Auto-subscription** - Server could automatically subscribe clients to commands they issue (tracked by API key)
3. **Interactive shell** - A new endpoint for PTY-based interactive sessions
4. **SSE fallback** - Alternative endpoint for clients that can't use WebSockets
5. **Filtered logs** - Query parameters for log filtering (grep, time range, etc.)
6. **Log persistence** - Store container logs in SQLite for historical access

---

## Files to Modify/Create

### New Files
- `orchestrator/routers/websockets.py` - WebSocket endpoints
- `orchestrator/connection_manager.py` - Connection state management
- `tests/test_websockets.py` - WebSocket tests

### Modified Files
- `orchestrator/app.py` - Add WebSocket router
- `orchestrator/socket_manager.py` - Change subscriber model to per-container
- `orchestrator/docker_client.py` - Add log streaming
- `orchestrator/models.py` - Add WebSocket message models (optional)
- `TODO.md` - Update status
- `docs/decisions/` - Add implementation notes (optional)

---

## Open Questions

1. Should the server automatically subscribe clients to commands they issue?
   - **Decision:** No, explicit subscription is cleaner. Client can subscribe immediately after receiving command_id.
   
2. Should historical command output be replayed on WebSocket connect or subscribe?
   - **Decision:** No. The WebSocket only streams new output. Clients use the existing `GET /containers/{id}/exec/{command_id}` REST endpoint to fetch historical output. This avoids overwhelming the connection with potentially large amounts of data and keeps concerns separated: WebSocket for real-time, REST for history.
   
3. Should container logs be persisted like command output?
   - **Decision:** No, for now they are ephemeral. Can add persistence later if needed.

4. How to handle very slow WebSocket consumers?
   - **Decision:** Apply backpressure - drop messages for slow consumers after a buffer limit, or close the connection

5. Should multiple WebSocket connections to the same container be allowed?
   - **Decision:** Yes, each client can have its own connection. Useful for multiple browser tabs or services.

---

## Follow-Up Items

Items to reconsider after initial implementation:

### Authentication Mechanism
The current plan uses header-based authentication (`X-API-Key`), which works for programmatic clients but **browsers cannot set custom headers on WebSocket connections**. After implementation, reconsider:
- Adding token-based auth via query parameter (`?token=<jwt>`)
- Post-connect authentication message flow
- Cookie-based authentication for same-origin clients
- Update auth middleware to support WebSocket connections

### Dependencies
- [ ] Add `websockets` package to `orchestrator/requirements.txt` for production WebSocket support

### Message Protocol Enhancements
- [ ] Consider adding `unsubscribe` message type for long-lived connections
- [ ] Clarify backpressure strategy: buffer limits, signaling dropped messages to clients
- [ ] Add `dropped` message type: `{"type": "dropped", "command_id": "...", "count": N}`

### Documentation
- [ ] Clarify SocketManager is adding **new** subscriber functionality (not changing existing code)
- [ ] Document the data flow: Guest Agent → SocketManager → ConnectionManager → WebSocket Clients
- [ ] Specify when broadcast happens relative to DB write (before/after/concurrent)

### Potential Improvements
- [ ] Auto-subscription: Track commands by API key and auto-subscribe the issuing client
- [ ] SSE fallback endpoint for clients that can't use WebSockets
- [ ] Log persistence: Store container logs in SQLite for historical access
- [ ] Interactive shell endpoint for PTY-based sessions
