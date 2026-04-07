# Exec Command Flow

How commands are sent to micro-containers and how results get back to the caller.

## Participants

1. **Caller** — anything that talks to the orchestrator REST API
2. **Orchestrator** — the FastAPI app, manages sockets and buffers output
3. **Guest agent** — a process inside the micro-container, connected to the Unix socket

## Flow

```
Caller                    Orchestrator                   Guest Agent
  |                            |                              |
  |  POST /containers/{id}/exec                               |
  |  { "command": "git clone ..." }                           |
  |--------------------------->|                              |
  |                            |  generate command_id         |
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
  |                            |  (buffer in memory)          |
  |                            |                              |
  |                            |  {"type":"output",           |
  |                            |   "id":"abc123",             |
  |                            |   "stream":"stderr",         |
  |                            |   "data":"Receiving..."}     |
  |                            |<-----------------------------|
  |                            |  (buffer in memory)          |
  |                            |                              |
  |                            |  {"type":"result",           |
  |                            |   "id":"abc123",             |
  |                            |   "exit_code":0}             |
  |                            |<-----------------------------|
  |                            |  (mark command complete)     |
  |                            |                              |
  |  GET /containers/{id}/exec/abc123                         |
  |--------------------------->|                              |
  |  { "command_id": "abc123", |                              |
  |    "status": "complete",   |                              |
  |    "stdout": "Cloning...", |                              |
  |    "stderr": "Receiving..",|                              |
  |    "exit_code": 0 }        |                              |
  |<---------------------------|                              |
```

## Key Details

### Command state lives in memory, not the database

The `containers` table tracks container lifecycle (running, stopped, etc.). Command execution state — stdout, stderr, exit code, completion status — is buffered **in memory** inside the socket manager, keyed by command ID.

This means:

- If the orchestrator restarts, in-flight command state is lost. The containers themselves may still be running, but command tracking resets.
- Each container can have **multiple commands in flight** simultaneously. They are independent, each with their own command ID.
- The database is not involved in exec beyond tracking `last_seen` (updated on every inbound socket message, including heartbeats).

### Polling, not streaming (for now)

The caller gets a command ID back immediately and polls `GET /containers/{id}/exec/{cmd_id}` to check progress. The response includes:

| Field | Description |
|---|---|
| `command_id` | The ID returned from the exec call |
| `status` | `pending` (sent, no output yet), `running` (output received), `complete` (exit code received) |
| `stdout` | Accumulated stdout as a single string |
| `stderr` | Accumulated stderr as a single string |
| `exit_code` | `null` until status is `complete` |

Streaming (SSE or WebSocket) is listed as an open design question in the README and is not part of the initial implementation.

### Socket protocol is newline-delimited JSON

One JSON object per line over the Unix socket at `/run/orchestrator.sock` inside the container. The orchestrator creates the socket file before starting the container. The guest agent connects once at startup and maintains a persistent connection.

### Heartbeats are separate from commands

The guest agent sends `{"type": "heartbeat"}` on its own schedule. These have no command ID — they just update `last_seen` on the container row in SQLite, keeping the idle-timeout reaper from stopping the container.
