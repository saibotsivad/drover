# Interactive Exec (`drover exec <container-id>`)

**Date:** 2026-05-25  
**Status:** In design — approach revised for session persistence

## Context

Non-interactive exec (`drover exec <container-id> -- <command>`) is working:
the CLI creates an exec record via REST, opens a WebSocket for output
streaming, and the guest executor runs the command and sends back results.

The CLI already has a stub in `exec.go` that returns
`interactive_exec_unsupported` when `--` is absent.

## Goal

`drover exec <container-id>` (no `--`) opens a full interactive terminal
session with the following properties:

- Raw PTY I/O with terminal resize support
- **Session outlives the CLI connection** — closing the terminal or a
  network glitch does not kill the in-container process
- **Snapshot on re-attach** — reconnecting shows the current terminal
  state without replaying the full session history (which may be large
  after a long-running AI agent session)
- **Follow mode** — after the snapshot, live output streams normally
- Day-long or longer sessions must remain resumable

## Why Docker exec alone is insufficient

The straightforward approach — use `docker exec` with `Tty: true`, proxy
bytes through the orchestrator, expose via WebSocket — fails the session
persistence requirement.

When the orchestrator holds the Docker exec attach connection:
- A CLI disconnect is survivable: the orchestrator keeps its Docker exec
  connection alive, the shell sees nothing.
- But if the orchestrator itself restarts, the Docker exec attach
  connection drops, the PTY gets SIGHUP, and the shell exits.

Even setting aside orchestrator restarts, there is no way to reconstruct
the current terminal state on re-attach without replaying everything
emitted since session start — potentially hours of AI agent output.

PTY support under `runsc` (gVisor) has been validated; the limitation
here is architectural, not gVisor-specific.

---

## Approach: In-Container PTY Session Manager

The container runs a lightweight PTY session manager process — similar in
role to a single tmux window — that:

1. Owns the PTY and the shell process.
2. Maintains a virtual terminal screen (parses ANSI/VT100 in-process).
3. Keeps a bounded scrollback buffer.
4. Connects to the orchestrator and streams output on demand.
5. Survives orchestrator disconnects by retrying the socket connection.

The orchestrator is the central broker — all CLI traffic flows through it.
The CLI never connects to the container directly.

```
CLI (raw mode)
  <-- binary WS frames: PTY output
  --> binary WS frames: stdin
  <-> JSON WS frames: control (snapshot, resize, exit)
        |
   Orchestrator
        |
  <-- pty_output JSON (base64)
  --> pty_input / pty_resize / pty_snapshot_req / pty_follow / pty_pause
        |
  /run/orchestrator.sock  (existing Unix socket, new role multiplexing)
        |
   PTY Manager process (in container)
        |
   PTY master fd ← fork → shell / AI agent
```

---

## Extended Socket Protocol: Role Multiplexing

The existing Unix socket (`/run/orchestrator.sock`) is already served by
`asyncio.start_unix_server`, which handles multiple simultaneous
connections.  Currently a new connection replaces the previous one for a
given container; this is relaxed to allow multiple typed connections.

Each connection announces itself with a `hello` message as its first line:

```json
{"type": "hello", "role": "executor"}
{"type": "hello", "role": "pty", "session_id": "<ulid>"}
```

The socket manager routes messages to the appropriate handler based on
which connection they arrive on.  The executor connection is unchanged.

### New messages: PTY manager → orchestrator

| Message | Fields | Meaning |
|---|---|---|
| `hello` | `role: "pty"`, `session_id`, `cols`, `rows` | Announce connection (or reconnect) |
| `pty_output` | `session_id`, `data` (base64), `seq` | Raw PTY bytes |
| `pty_snapshot` | `session_id`, `screen` (ANSI string) | Current virtual screen dump |
| `pty_exit` | `session_id`, `exit_code` | Shell process exited |

### New messages: orchestrator → PTY manager

| Message | Fields | Meaning |
|---|---|---|
| `pty_follow` | `session_id` | Start streaming `pty_output` messages |
| `pty_pause` | `session_id` | Stop streaming (no client connected) |
| `pty_input` | `session_id`, `data` (base64) | Stdin bytes from CLI |
| `pty_resize` | `session_id`, `cols`, `rows` | Resize the PTY |
| `pty_snapshot_req` | `session_id` | Request a terminal snapshot |
| `pty_terminate` | `session_id` | Kill the PTY session |

### New message: orchestrator → executor (launch)

| Message | Fields | Meaning |
|---|---|---|
| `pty_start` | `session_id`, `cols`, `rows` | Spawn the PTY manager subprocess |

---

## PTY Manager: Internal Design

The PTY manager is a new module in the `drover-executor` package
(`drover_executor/pty_manager.py`), invokable as a subprocess.

**Launch:** The executor receives `pty_start`, spawns:
```python
subprocess.Popen(
    [sys.executable, "-m", "drover_executor.pty_manager",
     session_id, str(cols), str(rows)],
    start_new_session=True,   # detach from executor's process group
)
```
`start_new_session=True` ensures the PTY manager survives executor
shutdown or SIGTERM sent only to the executor's process group.

**PTY fork:** The manager calls `pty.fork()`, sets the initial window
size via `TIOCSWINSZ`, then `exec`s `/var/run/drover/pty` in the child.
`/var/run/drover/pty` is a thin wrapper (default: `exec /bin/bash`) that
image authors can replace for env setup, banners, or policy gates.

**Virtual screen:** The manager feeds all PTY output through `pyte`
(Python terminal emulator) to maintain a live virtual screen.  On a
`pty_snapshot_req`, it serialises the current screen to an ANSI escape
sequence string and sends `pty_snapshot`.

**Streaming:** In `paused` state the manager reads PTY output and updates
the virtual screen but does not forward bytes to the orchestrator.  In
`following` state it forwards each chunk as `pty_output`.

**Reconnection:** If the orchestrator connection drops, the manager keeps
the PTY running and retries the socket connection with exponential backoff.
On reconnect it sends `hello` with the same `session_id`; the orchestrator
recognises this as a live session returning rather than a new one.

---

## Session Lifecycle

```
States: starting → active → orphaned → ended
```

| Trigger | Transition |
|---|---|
| CLI calls POST /containers/{id}/pty | `starting`: session record created, `pty_start` sent to executor |
| PTY manager sends `hello` | `active`: orchestrator routes `pty_follow`, CLI gets snapshot + stream |
| CLI WebSocket closes | `orphaned`: orchestrator sends `pty_pause`; PTY keeps running |
| CLI re-connects | `active` again: `pty_snapshot_req`, then `pty_follow` |
| PTY manager sends `pty_exit` | `ended`: orchestrator broadcasts exit code to any CLI, closes WS |
| CLI sends terminate request | orchestrator sends `pty_terminate`; PTY manager kills shell → `pty_exit` |
| Orchestrator restarts | PTY manager retries socket; on reconnect orchestrator rebuilds session in `orphaned` state |

**Session discovery:** `GET /containers/{id}/pty` lists sessions for a
container.  The CLI attaches to the single existing session (v1: one
session per container) or creates a new one if none exists.

---

## WebSocket Protocol (CLI ↔ Orchestrator)

Endpoint: `GET /containers/{container_id}/pty`

Auth: same as the existing `/ws` endpoint (Bearer header or `?token=`).

**Server → client:**
- **Binary frames**: raw PTY output bytes (no encoding overhead)
- **JSON text frames**: control messages
  - `{"type": "attached", "session_id": "...", "cols": N, "rows": N}` — sent immediately on accept
  - `{"type": "snapshot", "screen": "...ANSI..."}` — current terminal state, sent before follow begins
  - `{"type": "exit", "code": N}` — shell exited
  - `{"type": "error", "message": "..."}` — error conditions

**Client → server:**
- **Binary frames**: raw stdin bytes (key presses); passed directly to PTY
- **JSON text frames**: control messages
  - `{"type": "resize", "cols": N, "rows": N}` — terminal resize

The initial resize dimensions are sent by the CLI immediately after the
`attached` message arrives.  SIGWINCH on the CLI side triggers a `resize`
message.

---

## Component Changes

| Component | Changes |
|---|---|
| `executor/drover_executor/pty_manager.py` | New module: PTY fork, pyte screen, socket client, reconnection loop |
| `executor/drover_executor/agent.py` | Handle `pty_start` message → spawn PTY manager subprocess |
| `executor/pyproject.toml` | Add `pyte` dependency |
| `orchestrator/socket_manager.py` | Role-multiplexed connections; route PTY messages; `hello` dispatch |
| `orchestrator/container_manager.py` | `start_pty_session`, PTY session state tracking |
| `orchestrator/routers/pty.py` | New: `GET /containers/{id}/pty` WebSocket endpoint (bidirectional) |
| `orchestrator/models.py` | `PtySession` model |
| `cli/internal/commands/exec.go` | Interactive mode: raw terminal, SIGWINCH, bidirectional WS, snapshot display |

---

## Open Questions

1. **`pyte` in the executor package:** Adding `pyte` breaks the
   zero-external-dependency policy.  Alternatives: a minimal VT100 screen
   tracker written in-house, or moving terminal state tracking to the
   orchestrator (which parses all received `pty_output` bytes with `pyte`
   on its side, avoiding a container dep).  The orchestrator-side approach
   loses state on restart unless persisted; the in-container approach is
   more resilient but adds a dep.

2. **Snapshot format:** The `screen` field in `pty_snapshot` is described
   as an ANSI string.  Should this be the full screen (including invisible
   cells), only non-empty rows, or a structured cell grid?  The CLI needs
   to display it without artefacts from prior content — a full clear + redraw
   sequence is the safe option but may flicker.

3. **Scrollback vs. screen-only:** v1 proposes snapshotting only the
   visible screen.  Should a bounded scrollback buffer also be included?
   For AI agent sessions the user may want to scroll up on reconnect.

4. **Orphaned session TTL:** Orphaned sessions (no CLI client) hold a PTY
   and a process alive indefinitely.  Should the orchestrator auto-terminate
   orphaned sessions after a configurable timeout?  If so, the PTY manager
   needs a `pty_terminate` path that is triggered by a timer, not just by
   explicit CLI request.

5. **Orchestrator-side session persistence (SQLite):** Should session
   records survive orchestrator restart via the database?  If not, the
   orchestrator rebuilds session state purely from reconnecting PTY managers
   (via `hello` with a known `session_id`).  The DB approach would also
   allow `GET /containers/{id}/pty` to return sessions even before the PTY
   manager has reconnected after an orchestrator restart.

6. **Multiple sessions per container:** v1 proposes one session per
   container for simplicity.  If multiple are later supported, the CLI needs
   a way to list and select among them (`drover pty ls <container-id>`,
   `drover pty attach <session-id>`).
