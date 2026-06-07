# Interactive Sessions

`drover exec <container-id>` with no command drops the operator into an
interactive shell inside a micro-container: a real PTY, bidirectional
stdin/stdout, terminal resize handling, and an exit code that mirrors the
shell.

This document describes the lifecycle of an interactive session. The session is
organized around its **orchestrator ↔ guest transport**: a dedicated Unix-domain
socket per session, created inside the per-container socket folder, separate from
the socket that carries ordinary command traffic.

## Two planes

Every session uses two sockets at once:

- **Control plane** — the shared per-container `orchestrator.sock`. It carries
  session control as a single ordered channel per container, alongside ordinary
  command traffic.
- **Data plane** — the per-session socket. A bidirectional pipe carrying PTY
  bytes in both directions. The orchestrator forwards these bytes verbatim and
  never parses them.

A session is identified by a `session_id`, present in every control-plane message.

## Socket layout

The orchestrator is the Unix-socket **server** for every socket; the guest is the
**dialer**. Each container has its own socket folder containing the main socket
and a `sessions/` subfolder, one socket file per session:

```
/var/run/drover/sockets/{container_id}/
  ├── orchestrator.sock          # control plane (shared per container)
  └── sessions/
        └── {session_id}.sock    # data plane (one per session)
```

Inside the container the same tree is visible at `/var/run/drover/sockets/`. The
guest derives a session's socket path from this fixed root —
`/var/run/drover/sockets/sessions/{session_id}.sock` — rather than from any path
supplied by the orchestrator. Because the per-container folder is bind-mounted as
a directory, session sockets the orchestrator creates after the container starts
appear inside the container automatically.

The per-container folder and its `sessions/` subfolder are traversable by the
guest process. Each session socket is created with the same world-readable and
-writable permissions as the main socket.

## Lifecycle

A session moves through start, transmission (optionally paused and resumed any
number of times), and termination. It is **container-lifetime scoped**: it lives
only as long as its container runs, because the shell, PTY, and terminal emulator
are in-guest state that die with the container.

### Start

1. The orchestrator creates and listens on the session socket at
   `…/{container_id}/sessions/{session_id}.sock` **before** announcing it, so it
   is listening when the guest dials.
2. The orchestrator sends `session_start` over the control plane.
3. The guest dials its in-container session path, allocates a PTY, launches the
   operator's login shell, and starts an in-memory terminal emulator that
   consumes the shell's PTY output and maintains an authoritative screen model.
   The dial itself signals success.
4. A guest that cannot honor the session replies `session_rejected` on the
   control plane and does not dial.

A session is started exactly once. There is no separate "attach" step;
reconnecting a client is handled by pause/resume, below.

There is no start timeout. A guest that neither dials nor replies
`session_rejected` simply produces no data; the operator who started the session
ends it and starts a new one.

### Transmitting

The guest feeds PTY output into the emulator at all times, so the screen model is
always current. While transmitting, the guest first sends a full screen
**snapshot** over the data plane, then streams live PTY output. The default state
of a freshly started session is transmitting.

The snapshot carries the **visible screen only**. Scrollback is not preserved: a
client that connects or reconnects sees the current screen, not the history that
produced it.

On the data plane:

- **guest → client:** the snapshot frame, then live PTY output.
- **client → guest:** `stdin` bytes and terminal `resize` events.

The guest applies a `resize` to both the PTY and the emulator screen, so
subsequent snapshots are correctly dimensioned.

### Pause and resume

PTY transmission is gated independently of session liveness. The session keeps
running — shell, PTY, and emulator all alive and the screen model current —
regardless of whether the guest is transmitting. The session socket stays open
throughout.

- `session_pty_pause` — the guest stops sending PTY output but keeps reading the
  shell and feeding the emulator.
- `session_pty_resume` — the guest sends a fresh snapshot, then resumes streaming
  live output.

Pause and resume are driven by the orchestrator's view of the client: the
orchestrator pauses when the client disconnects and resumes when a client
(re)connects. Because the emulator is kept current even while paused, resume
always reflects the true present screen, no matter how much output the shell
produced while paused. "Paused" never means the session ended.

A paused session keeps a live shell and emulator running with no client
attached, and nothing reaps one whose client never returns. To make such a
session visible, the orchestrator tracks two coarse per-session timestamps: the
last time it received data from the client, and the last time the guest sent
data. These are approximate — enough to tell that a session has been idle for
hours or days — and exist so an operator can find and end abandoned sessions.

### Termination

A session ends in one of two ways.

**Orchestrator-initiated.** The orchestrator sends `session_terminate` on the
control plane. The guest stops sending, tears down the shell, PTY, and emulator,
and replies `session_terminated`. Only after that ack does the orchestrator
unlink the session socket file and close its server, so the file is never removed
out from under a still-writing guest.

**Guest-initiated (shell exits).** The shell's exit code reaches the client as
ordinary PTY output on the data plane — the orchestrator forwards it like any
other terminal output. The guest also sends `session_pty_stop`, carrying the exit
code, on the control plane. The orchestrator observes that message to unlink the
session socket file and close the client connection.

## Control-plane messages

All control-plane messages carry `session_id`.

**Orchestrator → guest:**

| Type                | Payload                  | Meaning                                                    |
| ------------------- | ------------------------ | --------------------------------------------------------- |
| `session_start`     | `session_id`             | Create the session and dial the data-plane socket.        |
| `session_pty_pause` | `session_id`             | Stop transmitting PTY output; keep the session running.   |
| `session_pty_resume`| `session_id`             | Send a fresh snapshot, then resume live output.           |
| `session_terminate` | `session_id`             | End the session; the guest acks before the socket is removed. |

**Guest → orchestrator:**

| Type                 | Payload                       | Meaning                                                       |
| -------------------- | ----------------------------- | ------------------------------------------------------------ |
| `session_rejected`   | `session_id`, `reason`        | The guest cannot honor the session; it does not dial.        |
| `session_terminated` | `session_id`                  | Ack of `session_terminate`; the orchestrator may now unlink. |
| `session_pty_stop`   | `session_id`, `exit_code`     | The shell exited on its own; the orchestrator tears down.    |

Successful start has no ack — the guest dialing the data-plane socket is the
signal.

## Cleanup

- The session socket persists for the whole session, from start to termination.
  Pause and resume never touch the socket file.
- **On terminate:** the orchestrator unlinks the session socket and closes its
  server only after the guest's `session_terminated` ack.
- **On shell exit:** the orchestrator unlinks the session socket and closes the
  client connection upon `session_pty_stop`.
- **On container stop:** all session sockets are closed and unlinked and every
  session is considered gone. Neither active nor paused sessions survive a stop,
  because the shell processes and emulator state die with the container. The
  container folder and `orchestrator.sock` are kept for resume.
- **On container destroy:** the entire `{container_id}/` tree, including
  `sessions/`, is removed.

A session socket left behind by an orchestrator crash is ignored; it is removed
when its container is destroyed and the whole tree goes away.

## Concurrency

A container may run any number of simultaneous interactive sessions. There are
no built-in limits on session count, and no separate controls beyond the
resources the operator gives the container.

## Capability

Interactive sessions require the container's image to advertise the
`interactive` capability in its `drover.capabilities` label. The orchestrator is
the authoritative gate and refuses sessions for images that do not advertise it.
See `docs/capabilities.md`.

## Related

- `docs/exec-commands.md`
- `docs/capabilities.md`
- `docs/cli.md`
- `docs/websockets.md`
