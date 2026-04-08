# Exec Command Flow

How commands are sent to micro-containers and how results get back to the caller.

## Participants

1. **Caller** — anything that talks to the orchestrator REST API
2. **Orchestrator** — the FastAPI app, manages sockets and persists output to SQLite
3. **Guest agent** — a process inside the micro-container, connected to the Unix socket

## Flow

```
Caller                    Orchestrator                   Guest Agent
  |                            |                              |
  |  POST /containers/{id}/exec                               |
  |  { "command": "git clone ..." }                           |
  |--------------------------->|                              |
  |                            |  generate command_id         |
  |                            |  INSERT into commands table  |
  |  { "command_id": "abc123" }|                              |
  |<---------------------------|                              |
  |                            |  write to socket:            |
  |                            |  {"type":"command",           |
  |                            |   "id":"abc123",             |
  |                            |   "exec":"git clone ..."}    |
  |                            |----------------------------->|
  |                            |                              |
  |                            |       (guest runs command)   |
  |                            |                              |
  |                            |  {"type":"output",           |
  |                            |   "id":"abc123",             |
  |                            |   "stream":"stdout",         |
  |                            |   "data":"Cloning..."}       |
  |                            |<-----------------------------|
  |                            |  INSERT into command_messages |
  |                            |                              |
  |                            |  {"type":"output",           |
  |                            |   "id":"abc123",             |
  |                            |   "stream":"stderr",         |
  |                            |   "data":"Receiving..."}     |
  |                            |<-----------------------------|
  |                            |  INSERT into command_messages |
  |                            |                              |
  |                            |  {"type":"result",           |
  |                            |   "id":"abc123",             |
  |                            |   "exit_code":0}             |
  |                            |<-----------------------------|
  |                            |  UPDATE commands SET          |
  |                            |    status='complete',        |
  |                            |    exit_code=0               |
  |                            |                              |
  |  GET /containers/{id}/exec/abc123                         |
  |--------------------------->|                              |
  |  { "command_id": "abc123", |                              |
  |    "status": "complete",   |                              |
  |    "exit_code": 0,         |                              |
  |    "messages": [           |                              |
  |      {"seq":1,             |                              |
  |       "stream":"stdout",   |                              |
  |       "data":"Cloning.."}, |                              |
  |      {"seq":2,             |                              |
  |       "stream":"stderr",   |                              |
  |       "data":"Receiving"}  |                              |
  |    ] }                     |                              |
  |<---------------------------|                              |
```

## Database Schema

### `commands` table

One row per exec invocation.

| Column | Type | Description |
|---|---|---|
| `id` | TEXT PK | The command ID returned to the caller |
| `container_id` | TEXT FK | References `containers(id)` |
| `command` | TEXT | The exec string sent to the guest |
| `status` | TEXT | `pending`, `running`, or `complete` |
| `exit_code` | INTEGER | `NULL` until status is `complete` |
| `created_at` | TEXT | ISO 8601 timestamp |

Indexed on `container_id` for listing all commands on a container.

### `command_messages` table

One row per output message from the guest agent.

| Column | Type | Description |
|---|---|---|
| `seq` | INTEGER PK | Auto-incrementing sequence for ordering |
| `command_id` | TEXT FK | References `commands(id)` |
| `stream` | TEXT | `stdout` or `stderr` |
| `data` | TEXT | The output payload |
| `received_at` | TEXT | ISO 8601 timestamp |

Indexed on `command_id` for fast retrieval of all messages for a command.

## Key Details

### Command state is persisted to SQLite

Both the command metadata and every output message are written to the database as they arrive. This means:

- Command history **survives orchestrator restarts**. If the orchestrator goes down and comes back, all prior output is still queryable.
- Each container can have **multiple commands in flight** simultaneously. They are independent, each with their own command ID and message stream.
- Messages are ordered by `seq` (auto-incrementing), preserving the interleaved stdout/stderr order as it happened.

### Polling, not streaming (for now)

The caller gets a command ID back immediately and polls `GET /containers/{id}/exec/{cmd_id}` to check progress. The response includes:

| Field | Type | Description |
|---|---|---|
| `command_id` | string | The ID returned from the exec call |
| `status` | string | `pending` (sent, no output yet), `running` (output received), `complete` (exit code received) |
| `exit_code` | int or null | `null` until status is `complete` |
| `messages` | array | Ordered list of `{seq, stream, data}` objects |

Streaming (SSE or WebSocket) is listed as an open design question in the README and is not part of the initial implementation.

### Socket protocol is newline-delimited JSON

One JSON object per line over the Unix socket at `/run/orchestrator.sock` inside the container. The orchestrator creates the socket file before starting the container. The guest agent connects once at startup and maintains a persistent connection.

### Heartbeats are separate from commands

The guest agent sends `{"type": "heartbeat"}` on its own schedule. These have no command ID — they just update `last_seen` on the container row in SQLite, keeping the idle-timeout reaper from stopping the container.
