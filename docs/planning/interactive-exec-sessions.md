# Interactive Exec Sessions

**Status:** Draft — Axis 1 (orchestrator↔guest transport) decided and its
per-container socket-folder groundwork already landed in `main`; the session
sockets on top of it, plus Axes 2–4, are still to build.

## Goal / Desired Outcome

`drover exec <container-id>` (no `-- <command>`) drops the operator into an
interactive shell inside the micro-container: a real PTY, bidirectional
stdin/stdout, terminal resize handling, and an exit code that mirrors the
shell. `drover exec <id> -- <cmd>` keeps working exactly as it does today.

## Background

Read first: the exec flow (`docs/exec-commands.md`) and the WebSockets ADR
(`docs/decisions/2026-04-11-websockets-for-streaming.md`), which explicitly
chose WebSockets partly to leave the door open for "attach to an interactive
shell" and "send stdin to a running command."

Current state (the constraints that shape the work below):

- **Per-container socket folder, mounted as a directory.** The orchestrator is
  the Unix-socket *server*; the guest agent dials *out* to
  `/var/run/drover/sockets/orchestrator.sock`. Each container gets its own
  folder `/var/run/drover/sockets/{container_id}/` (orchestrator in-container
  path), bind-mounted as a directory onto `/var/run/drover/sockets/` in the
  guest, with `orchestrator.sock` inside it. Because it is a *directory* mount,
  additional per-container sockets created after container start are visible
  inside the container — the socket_manager docstring already calls out
  interactive session sockets as the intended use.
- **Fire-and-forget, no TTY.** `runner.run_command` uses
  `create_subprocess_shell(..., stdin=DEVNULL)`. No PTY, no stdin, no resize.
- **WS is one-way.** `/containers/{id}/ws` only sends server→client today; it
  fans Docker logs + exec output into per-connection queues
  (`connection_manager.py`). There is no client→server path.
- **Exec is capability-gated.** `container_manager._assert_capability` rejects
  with `422` unless the image's `drover.capabilities` label includes the key
  (`docs/capabilities.md`). Interactive will want its own key.
- **CLI already stubs it.** `cli/internal/commands/exec.go` returns
  `interactive_exec_unsupported` when no `--` is present.

## Problem

Interactive PTY traffic is a fundamentally different shape than the existing
command model: long-lived, bidirectional, latency-sensitive, binary-ish, and
1:1 with a client. The current stack (per-container socket folder, one-way WS,
no-stdin runner) supports the command model but none of the interactive needs
directly. The design question is *where* to add the bidirectional, per-session
plumbing.

There are **three independent axes**. Axis 1 is decided; Axes 2–4 still list
options where there is no single obviously-correct answer.

---

## Axis 1 — Orchestrator ↔ guest transport — **DECIDED**

**Decision (team): dedicated per-session socket inside the per-container socket
folder.** The folder-per-container bind mount this depends on has already
landed in `main` (gVisor behaviour with a directory mount of Unix sockets is
verified). What remains is adding a `sessions/` subfolder and one socket per
interactive session. The win is clean, independently-managed streams: command
traffic and each interactive session each get their own connection, with no
shared framing and no head-of-line blocking.

### Layout

Three distinct path namespaces are in play; keep them straight:

- **Host path** — wherever `DROVER_SOCKETS_DIR` resolves to on the host (e.g.
  `./sockets/`). It does **not** have to equal the in-container path. The
  orchestrator discovers it at startup by self-inspecting its own container's
  Docker `Mounts` (the `Source` of the mount whose `Destination` is
  `/var/run/drover/sockets/`). This is `self._host_socket_dir` in
  `container_manager.py`. It matters because Docker resolves *nested*
  bind-mount sources against the **host** filesystem, so the bind *source* must
  be the host path, not the orchestrator's own path.
- **Orchestrator in-container path** — fixed `SOCKET_DIR =
  /var/run/drover/sockets/`. Where the orchestrator actually creates the
  socket files (`start_unix_server`).
- **Guest in-container path** — also `/var/run/drover/sockets/`, where the
  per-container folder is mounted. Same for every container.

What is **already in place**: the orchestrator creates the per-container folder
at its in-container path `os.path.join(SOCKET_DIR, container_id)` with
`orchestrator.sock` (`ORCHESTRATOR_SOCKET_NAME`) inside, and bind-mounts the
**host** mirror `os.path.join(self._host_socket_dir, container_id)` as the
source onto the guest in-container path `/var/run/drover/sockets/`. The guest
dials `/var/run/drover/sockets/orchestrator.sock`.

What **will be added** is the `sessions/` subfolder and per-session sockets:

```
orchestrator in-container:  /var/run/drover/sockets/{container_id}/
host (bind source):         {host_socket_dir}/{container_id}/
       (the same dir, surfaced via the per-container sockets-dir mount)
                              ├── orchestrator.sock     # main orch <-> guest  (exists today)
                              └── sessions/             # NEW
                                    └── {session-id}.sock  # NEW — one per session

guest in-container:         /var/run/drover/sockets/   (same path every container)
                              ├── orchestrator.sock
                              └── sessions/{session-id}.sock
```

- The session sockets reuse the exact host-vs-in-container split already used
  for `orchestrator.sock` — they just live one level deeper, under
  `sessions/`. The existing self-inspection already supplies
  `self._host_socket_dir`, so **no new host-path discovery is needed**.
- Because the per-container folder is mounted as a **directory**, session
  socket files the orchestrator creates *after* container start appear inside
  the container automatically — this is exactly the headroom the folder layout
  was built to provide.
- The orchestrator must create the `sessions/` subdir (and clean it up); see
  Cleanup semantics for how that interacts with the existing
  `create_socket`/`destroy_socket`.

### Roles and ordering

The orchestrator stays the Unix-socket **server** for every socket; the guest
stays the **dialer** — consistent with the main socket. For an interactive
session:

1. Orchestrator creates the session socket server at
   `…/{container_id}/sessions/{session-id}.sock` (`start_unix_server`,
   `chmod` it) **before** announcing it, so it is listening when the guest
   dials.
2. Orchestrator sends a `session_start` lifecycle message over
   `orchestrator.sock`.
3. Guest dials its in-container session path, allocates a PTY, launches the
   shell, and starts an in-memory terminal emulator (see Axis 3 — likely
   `pyte`) that consumes the shell's PTY output and maintains an authoritative
   screen model. The dial itself signals success; a guest that cannot honor an
   interactive session instead replies `session_rejected` on the main socket
   (the orchestrator remains the authoritative capability gate — see Lifecycle
   protocol).
4. The session begins **transmitting**. The guest feeds PTY output into the
   emulator at all times (so the screen model is always current), and while
   transmitting sends a full screen **snapshot** first, then streams live PTY
   output over the session socket.
5. **PTY flow control is decoupled from session liveness.** The session keeps
   running from start to terminate regardless; what the orchestrator gates is
   only whether the guest *transmits* PTY output, based on its view of the WS
   client:
   - On WS client disconnect → `session_pty_pause`: the guest stops sending
     PTY output but keeps the shell, PTY, and emulator running and the screen
     model current. The session socket stays open.
   - On WS client (re)connect → `session_pty_resume`: the guest sends a full
     snapshot, then resumes streaming live output.
   The sole purpose of pause is to avoid flooding the orchestrator with PTY
   bytes it cannot deliver to a disconnected client. Because the emulator is
   updated even while paused, resume always reflects the true current screen,
   no matter how much output the shell produced in the meantime.
6. **Termination** ends the session:
   - *Orchestrator-initiated:* `session_terminate` on the main socket → the
     guest stops sending, tears down the shell/PTY/emulator, and replies
     `session_terminated` on the main socket → the orchestrator then unlinks
     the session socket file and closes its server. The ack ordering avoids
     removing the socket out from under a still-writing guest.
   - *Guest-initiated (shell exits):* the shell's exit code travels back as
     **normal PTY output over the session socket** — the orchestrator does not
     parse or intercept the data plane, it just forwards bytes, so the client
     sees the code the same way it sees any other terminal output. In addition,
     the guest sends `session_pty_stop` with the shell's exit code over the
     **main** socket; the orchestrator observes that to unlink the session
     socket file and close the client WS.

A running session is **container-lifetime scoped**: it lives only as long as
its container runs, since the shell and emulator are in-guest state that die
with the container (see Cleanup semantics). "Paused" never means the session
ended — only that PTY transmission is suspended; the session socket stays open
from start to terminate.

### Lifecycle protocol — two planes

Session traffic splits across two sockets:

- **Control plane** — the main `orchestrator.sock`. Carries all session
  control, as a single ordered channel per container.
- **Data plane** — the per-session socket. A dumb bidirectional pipe carrying
  PTY bytes both ways.

**Control plane, orch → guest** (new types alongside the existing `command`):

- `session_start` — `{"type":"session_start","session_id":"…"}`. Create the
  session. The guest derives the socket path itself from its known mount root
  (`/var/run/drover/sockets/sessions/{session_id}.sock`) rather than trusting a
  host-side path, so host/container path differences never leak across. (There
  is no separate "attach" — re-attach after a client reconnect is just
  `session_pty_resume`; a session is started exactly once.)
- `session_pty_pause` / `session_pty_resume` —
  `{"type":"session_pty_pause","session_id":"…"}`. Gate PTY *transmission*
  only; the session keeps running either way. `resume` makes the guest send a
  fresh snapshot before resuming live output.
- `session_terminate` — `{"type":"session_terminate","session_id":"…"}`. End
  the session; the guest acks before the orchestrator removes the socket file.

**Control plane, guest → orch:**

- `session_rejected` —
  `{"type":"session_rejected","session_id":"…","reason":"…"}`. The guest cannot
  honor the session (e.g. a custom agent without PTY support). Success needs no
  ack — the guest dialing the session socket is the signal.
- `session_terminated` — `{"type":"session_terminated","session_id":"…"}`. Ack
  of `session_terminate`; the orchestrator unlinks the socket only after this.
- `session_pty_stop` —
  `{"type":"session_pty_stop","session_id":"…","exit_code":N}`. Sent when the
  shell exits on its own. The orchestrator observes it to unlink the session
  socket file and close the client WS. (The exit code is *also* visible to the
  client as ordinary PTY output on the data plane — this main-socket message is
  the orchestrator's own signal to tear down, not the client's notification.)

**Data plane (session socket):** the snapshot frame and live PTY output flow
guest→client; `stdin` and `resize` flow client→guest. The orchestrator never
parses the data plane — it forwards bytes verbatim — so the shell's exit code
arrives at the client as normal PTY output, not a distinct frame. Framing (e.g.
base64 vs binary frames) is an Axis 3 detail.

Putting pause/resume on the control plane (not the session socket) keeps the
session socket a dumb pipe; the alternative is noted in open questions. The
default state of a freshly started session is transmitting.

### Cleanup semantics (extends `SocketManager`)

- `SocketManager` is currently keyed by `container_id` (one server/writer/task
  each). It gains a session dimension: a set of session servers/connections
  per container. This is a real but contained refactor.
- **The session socket persists for the whole session** (start → terminate). A
  paused session keeps its socket open — pause/resume never touch the socket
  file, only whether the guest writes PTY bytes to it.
- **On terminate (orch-initiated):** unlink `{session-id}.sock` and close its
  server **only after** the guest's `session_terminated` ack, so the file is
  never removed out from under a still-writing guest.
- **On shell exit (guest-initiated):** the guest sends `session_pty_stop` (with
  the exit code) on the **main** socket; the orchestrator then unlinks the
  session socket and closes the client WS. (The exit code also reaches the
  client as ordinary PTY output on the data plane.)
- **On container stop (resume-able):** `close_socket` keeps the folder and
  `orchestrator.sock` (as today, for resume); it must additionally close and
  unlink all session sockets and consider every session gone — neither active
  nor *paused* sessions survive a stop, because the shell processes and the
  in-guest emulator state die with the container.
- **On destroy:** `destroy_socket` must remove the whole `{container_id}/`
  tree **including `sessions/`**. ⚠️ Today it does `unlink(orchestrator.sock)`
  then `os.rmdir(container_dir)` (swallowing `OSError`); `rmdir` fails on a
  non-empty directory, so any leftover `sessions/{…}.sock` would silently
  orphan the folder. This needs to become a recursive removal (e.g.
  `shutil.rmtree`) or an explicit `sessions/` sweep before the `rmdir`.

### Permissions

The per-container directory and its `sessions/` subdir must be traversable by
the guest process (`o+rx`); session sockets get the same world-rw `chmod`
(`0o777`) the main socket gets today.

### Open questions / unresolved (Axis 1)

- **Stale session sockets.** If the orchestrator crashes mid-session, a
  `{session-id}.sock` can linger in the folder. Need a sweep of `sessions/` on
  container start/stop/destroy (mirroring the existing stale-`.sock` unlink in
  `create_socket`).
- **Guest start failure / race.** A guest that can't honor the session replies
  `session_rejected`, but a guest that neither dials nor rejects (mid-restart,
  hung) still needs a timeout + error path so the orchestrator doesn't hang or
  leave the client waiting.
- **Abandoned (paused) session GC.** A paused session keeps a live shell and
  emulator running with no client attached. What reaps one whose client never
  returns — a per-session idle timeout, an explicit operator/UI
  `session_terminate`, or the container's own idle reaper? And does an
  *unattached* running session still count as activity for the container
  idle-timeout reaper (it has no heartbeat of its own)?
- **Pause/resume plane.** Control plane (main socket, chosen here) vs. carrying
  pause/resume on the session socket itself. Main socket keeps the session
  socket a dumb pipe and gives one ordered control channel; revisit if that
  ordering ever fights with data-plane timing.
- **Snapshot fidelity.** Visible screen only, or include scrollback? `pyte`'s
  base `Screen` models just the visible grid; `HistoryScreen` adds scrollback
  at a memory cost. Pick what "resume" should show.
- **`pyte` as a dependency.** It breaks the executor's current zero-dependency
  posture. Confirm that's acceptable, or whether a vendored/minimal emulator is
  warranted.
- **Concurrency caps.** Whether `--max-concurrent-commands` applies to
  sessions, or sessions get their own cap, and what the guest does when over
  the limit (refuse the `session_start`?).
- **Path constant ownership.** The in-container path is already a *shared*
  contract: the orchestrator owns `SOCKET_DIR` + `ORCHESTRATOR_SOCKET_NAME`
  (`config.py`), while the executor independently hardcodes the same
  `/var/run/drover/sockets/orchestrator.sock` as its default. Adding
  `sessions/` + the session-socket naming extends that shared contract. Decide
  where it is defined so orchestrator and executor can't drift. (The host path
  stays separate and self-discovered — `DROVER_SOCKETS_DIR` /
  `self._host_socket_dir` — and is not part of this in-container contract.)

## Axis 2 — Client ↔ orchestrator transport

**Option 2A — Extend the existing `/containers/{id}/ws`.** Start reading
client→server frames on the same socket and route `stdin`/`resize` to the
session.
- *Pro:* one endpoint; the ADR anticipated exactly this.
- *Con:* that endpoint currently also multiplexes Docker logs for *all*
  watchers; overloading it with a 1:1 interactive session muddies its
  contract.

**Option 2B — New dedicated endpoint, e.g. `/containers/{id}/attach` (or
`/execs/{session_id}/attach`).** A purpose-built bidirectional WS for one
interactive session.
- *Pro:* clean, single-purpose contract; auth/lifecycle/close semantics don't
  have to coexist with the log-fanout endpoint; easier to reason about exit
  codes and resize.
- *Con:* second WS endpoint to maintain.

## Axis 3 — Executor PTY mechanics (new, regardless of the above)

A new capability in `drover-executor`:
- Allocate a PTY (`pty.openpty` / `os.openpty` + `create_subprocess_exec`, or
  `pty.fork`), launch the user's login shell (`$SHELL` or `/bin/sh`).
- Feed the shell's PTY output through an **in-memory terminal emulator**
  (candidate: `pyte`, a pure-Python VT-compatible `Screen`) which holds the
  authoritative screen model. The emulator is fed **continuously**, even while
  PTY transmission is paused, so the screen model is always current. That is
  what makes resume cheap: on `session_pty_resume` the guest renders the
  current screen to a **snapshot** frame and sends that, then streams live PTY
  output — no full byte-history replay. `pyte` is a **new external
  dependency**, which the executor has so far avoided (see the executor
  README's "zero external dependencies" claim) — a deliberate trade-off to
  weigh.
- Apply window size to **both** the PTY (`fcntl.ioctl(fd, termios.TIOCSWINSZ,
  …)`) and the emulator screen (`Screen.resize`) on `resize`, so the snapshot
  stays correctly dimensioned.
- `session_pty_pause` stops writing PTY bytes to the session socket but keeps
  reading/feeding the emulator; `session_pty_resume` re-sends the snapshot then
  resumes. Tear the shell/PTY/emulator down only on shell exit or
  `session_terminate`, and report the shell's exit status.
- Exposed as an override point on `Agent` (e.g. `on_attach`/`on_interactive`)
  consistent with the existing `on_command` hook.

## Axis 4 — CLI (Go) terminal handling

- Replace the `interactive_exec_unsupported` stub in `exec.go`.
- Put the local terminal into raw mode (`golang.org/x/term`), restore on exit.
- Dial the bidirectional WS, copy stdin→WS and WS→stdout, send `resize` on
  startup and on `SIGWINCH`, exit with the shell's code.
- Refuse / fall back gracefully when stdin is not a TTY (piped input).

---

## Recommendation (for discussion)

- **Axis 1: decided, partly built** — the per-container socket *folder* (with
  `orchestrator.sock` inside) has landed in `main`; remaining work is the
  `sessions/` subfolder and per-session sockets on top of it (see Axis 1
  above).
- **Axis 2: 2B** — a dedicated client WS endpoint keeps the overloaded
  log-fanout WS contract clean and gives interactive sessions their own
  lifecycle/exit semantics. Still open for discussion.
- **Axes 3 & 4** proceed regardless of the transport choices.

Add a capability key (proposed `interactive`, or `exec.interactive`) gated in
`container_manager` and advertised on executor-bearing images, per
`docs/capabilities.md`'s "Adding a new capability" steps.

## Open Questions

- **Capability granularity:** a distinct `interactive` key, or fold it into
  `exec`? (Leaning distinct — an image may ship a command-only executor.)
- **Session persistence:** are interactive sessions ephemeral-only, or do they
  get a row like `commands` for listing/audit? (Leaning no DB persistence — but
  note this is now nuanced: a session *is* resumable while its container runs,
  via the in-guest emulator snapshot, even though that state is never written to
  the DB and does not survive a container stop. So "ephemeral" means
  DB-ephemeral and container-lifetime-scoped, not non-resumable.)
- **Concurrency:** how many simultaneous interactive sessions per container,
  and how do they interact with `--max-concurrent-commands`?
- **Idle/heartbeat:** does an attached session count as activity for the
  idle-timeout reaper, and what closes a session abandoned by a dead client?
- **PTY output framing:** base64 vs. a binary WS frame type to avoid bloating
  interactive latency.
- **Auth on the new endpoint:** reuse the WS Bearer/`?token=` scheme from
  `websockets.py` verbatim.

## Risks and Mitigations

- **Recursive folder cleanup (Axis 1):** `destroy_socket`'s current `rmdir`
  won't remove a `sessions/` subtree; switch to a recursive removal or sweep
  `sessions/` first, and sweep stale session sockets on start/stop too.
- **Raw-mode terminal corruption:** always restore terminal state via
  `defer`, including on panic/signal.
- **Leaked PTYs / zombie shells:** kill the process group on
  disconnect/cancel, mirroring `runner.run_command`'s `killpg` on cancel.
- **Capability bypass:** enforce in the orchestrator, not just the CLI/webapp.

## Documentation Impact

- `docs/exec-commands.md` — document the interactive flow and message types.
- `docs/capabilities.md` — add the new capability row.
- `docs/cli.md` — document `drover exec <id>` interactive behaviour.
- `executor/README.md` — document the new PTY hook/override.
- `orchestrator/README.md` — document the new/extended WS endpoint.
- New ADR(s) once adopted: the transport choice (Axis 1) and the capability
  decision are both ADR-worthy.
