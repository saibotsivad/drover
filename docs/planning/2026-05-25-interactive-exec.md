# Interactive Exec (`drover exec <container-id>`)

**Date:** 2026-05-25  
**Status:** Draft — options, not a final plan

## Context

Non-interactive exec (`drover exec <container-id> -- <command>`) is working:
the CLI creates an exec record via REST, opens a WebSocket for output
streaming, and the guest executor runs the command and sends back results.

The CLI already has a stub in `exec.go` that returns
`interactive_exec_unsupported` when `--` is absent.  This document lays
out the options for implementing the interactive path.

## Goal

`drover exec <container-id>` (no `--`) opens a full interactive terminal
session: raw PTY I/O, terminal resize support, and a clean exit code on
shell exit.

## Key Constraints

1. **Socket is file-bind-mounted.**  
   `container_manager._init_container` mounts the socket as
   `{host_socket_path}:/run/orchestrator.sock`.  Socket files created
   *after* the container starts are not visible inside the container
   without changing this mount strategy.

2. **WebSocket transport is already bidirectional.**  
   The existing `/containers/{id}/ws` endpoint is one-directional in
   practice (server→client only), but the ADR explicitly anticipated
   using the other direction for stdin/interactive attach.

3. **gVisor (`runsc`) is used for standard containers.**  
   Docker exec works under runsc but is worth validating for PTY use.

---

## Options

### Option A — Docker exec API with PTY (no executor changes)

The Docker Engine API supports creating an exec instance with
`AttachStdin/AttachStdout/AttachStderr + Tty: true`, then attaching
to get a raw PTY stream.  The orchestrator proxies bytes directly
between that Docker socket connection and a WebSocket.

**Flow:**
```
CLI (raw mode) <--WS--> Orchestrator <--Docker socket--> Docker exec (PTY)
```

1. `drover exec <container-id>` → CLI opens a new WebSocket endpoint,
   e.g. `GET /containers/{id}/pty`.
2. Orchestrator calls `POST /containers/{docker_id}/exec` (TTY=true,
   `Cmd=["/bin/sh"]` or configurable) then `POST /exec/{exec_id}/start`.
3. Orchestrator spawns two coroutines: WS→Docker (stdin) and
   Docker→WS (stdout/stderr).
4. Resize: CLI detects SIGWINCH and sends a JSON control frame over
   WS; orchestrator calls `POST /exec/{exec_id}/resize?h=N&w=N`.
5. On shell exit the orchestrator closes the WS and returns the exit code.

**Orchestrator changes:** Add `create_exec` / `start_exec` / `resize_exec`
to `DockerClient`; add `GET /containers/{id}/pty` WebSocket endpoint.  
**CLI changes:** New interactive mode in `exec.go` (raw terminal, SIGWINCH,
bidirectional WS).  
**Executor changes:** None.

**Pros:**
- PTY semantics handled natively by Docker.
- Proven pattern (Portainer, k9s, Lens all do this).
- No executor or socket infrastructure changes.

**Cons:**
- Bypasses the executor library entirely; interactive and non-interactive
  exec use different code paths end-to-end.
- Need to decide what `Cmd` defaults to (`/bin/sh`, `$SHELL`, or caller-specified).
- gVisor PTY support should be confirmed before committing.

---

### Option B — New per-session socket (executor-based, requires mount change)

The orchestrator creates a session-specific socket file and tells the guest
to connect to it via the existing command channel.  Raw PTY bytes flow over
that dedicated connection.

**Requires first:** change the container bind-mount from a file mount to a
directory mount so that new socket files are visible inside running
containers:

```python
# current
binds = [f"{host_socket_path}:/run/orchestrator.sock"]

# proposed
binds = [f"{host_socket_dir}:/run/drover/sockets/"]
# executor then connects to /run/drover/sockets/{container_id}.sock
```

**Flow once the mount is a directory:**
```
CLI (raw mode) <--WS--> Orchestrator <--pty-{session}.sock--> Executor (PTY)
                              |--- command msg --> existing .sock ---^
```

1. `drover exec <container-id>` → CLI opens `GET /containers/{id}/pty` WS.
2. Orchestrator creates `/var/run/drover/sockets/{container_id}-pty-{session_id}.sock`
   and listens on it.
3. Orchestrator sends `{"type": "pty_start", "socket": "...pty-{session_id}.sock",
   "session_id": "...", "cols": N, "rows": N}` over the existing command socket.
4. Executor (new feature): connects to the named session socket, spawns a PTY
   shell, and pipes raw bytes both ways.
5. Orchestrator proxies the session socket ↔ WebSocket.
6. Resize: JSON control frame on WS → `{"type":"pty_resize", ...}` on session socket.

**Orchestrator changes:** Session socket creation/cleanup, new `pty_start`
message type, `GET /containers/{id}/pty` WS endpoint.  
**Executor changes:** New `pty_start` handler, session socket client, PTY
subprocess management (`pty` stdlib module or `openpty`).  
**CLI changes:** Same as Option A.

**Pros:**
- Consistent with the system's executor-centric philosophy.
- Executor library gains a reusable PTY capability.
- Clear separation: control channel vs. raw PTY channel.

**Cons:**
- Directory mount is a breaking change to container setup; existing images
  need to handle the new socket path (`/run/drover/sockets/{id}.sock`
  instead of `/run/orchestrator.sock`), or we keep a symlink/compat path.
- More moving parts: session socket lifecycle, cleanup on disconnect.
- `pty` module is Unix-only; executor already targets Linux so not a blocker.

---

### Option C — PTY multiplexed over existing socket

Add `pty_*` message types to the existing newline-delimited JSON protocol.
No new sockets or mount changes.

**New messages (guest ↔ orchestrator):**
- `{"type": "pty_start", "session_id": "...", "cols": N, "rows": N}` (orch→guest)
- `{"type": "pty_output", "session_id": "...", "data": "<base64>"}` (guest→orch)
- `{"type": "pty_input", "session_id": "...", "data": "<base64>"}` (orch→guest)
- `{"type": "pty_resize", "session_id": "...", "cols": N, "rows": N}` (orch→guest)
- `{"type": "pty_end", "session_id": "...", "exit_code": N}` (guest→orch)

**Pros:**
- No socket infrastructure changes.
- No mount strategy change.
- Executor gets PTY capability through a clean protocol extension.

**Cons:**
- PTY output can be high-frequency small chunks; wrapping each in a JSON
  envelope with base64 encoding adds latency and CPU overhead.
- Mixes raw PTY traffic with heartbeats and command results on one
  line-buffered connection; backpressure and head-of-line blocking become
  concerns.
- Harder to benchmark / debug than a dedicated socket.

---

## Summary

| | Docker exec (A) | New socket (B) | Multiplex (C) |
|---|---|---|---|
| Executor changes | None | PTY support | PTY support |
| Mount change | No | Yes (breaking) | No |
| Infrastructure | Docker exec API | Session sockets | None |
| PTY fidelity | Native | Native | Base64/JSON overhead |
| Conceptual purity | Separate path | Unified path | Unified path |

## Open Questions

1. **gVisor PTY:** Does `docker exec --tty` work correctly under `runsc`?
   Needs a quick smoke test before committing to Option A.

2. **Mount migration:** If we choose Option B, what is the migration path
   for existing executor images that hard-code `/run/orchestrator.sock`?
   (A compat symlink in the socket directory, or a new env var?)

3. **Default shell:** For both A and B, what command does the orchestrator
   launch — `/bin/sh`, `$SHELL`, or something caller-specified?

4. **Session limits:** Should we cap concurrent interactive sessions per
   container?
