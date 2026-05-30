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
3. Guest dials its in-container session path. For a **new** session it
   allocates a PTY, launches the shell, and starts an in-memory terminal
   emulator (see Axis 3 — likely `pyte`) that consumes the shell's PTY output
   and maintains an authoritative screen model. For a **re-attach** to an
   existing session (same `session_id`) it reuses the live PTY/shell/emulator.
4. On (re)connection the guest first emits a **snapshot** rendered from the
   emulator's current screen state, then streams live PTY output. This is the
   payoff of the emulator: a reconnecting client gets the current screen in one
   frame instead of a replay of the entire byte history.
5. Detach vs. terminate are **distinct**:
   - **Detach / pause** — the session socket closes (client gone) or the
     orchestrator sends `session_detach`. The guest stops forwarding but keeps
     the shell, PTY, and emulator state alive; the session survives, ready to be
     re-attached. The orchestrator unlinks the now-dead session socket.
   - **Terminate** — the shell exits on its own, or the orchestrator sends
     `session_terminate`. The guest tears down the PTY and drops the emulator
     state; the orchestrator unlinks the session socket and forgets the session.

Pause/resume here is **client-detach scoped, not container-stop scoped**: a
detached session lives only as long as its container keeps running, since the
shell and emulator are in-guest state that die with the container (see Cleanup
semantics).

### Lifecycle protocol (over the main `orchestrator.sock`)

New orch→guest message types alongside the existing `command`:

- `session_start` — `{"type":"session_start","session_id":"…"}`. Start a new
  session, or (if the guest already holds that `session_id`) re-attach to the
  existing one. The guest derives the socket path itself from its known mount
  root (`/var/run/drover/sockets/sessions/{session_id}.sock`) rather than
  trusting a host-side path, so host/container path differences never leak
  across. A separate `session_attach` could distinguish the two, but folding
  re-attach into `session_start` keeps the protocol smaller — see open
  questions.
- `session_detach` — `{"type":"session_detach","session_id":"…"}`. Pause:
  stop forwarding but keep the shell/PTY/emulator alive for later re-attach.
  (Socket close alone also implies detach.)
- `session_terminate` — `{"type":"session_terminate","session_id":"…"}`.
  Kill the shell and drop the session entirely.

The PTY byte stream, the reconnect **snapshot** frame, and `resize`/`exit`
messages flow over the **session socket**, not the main socket — framing for
that is an Axis 3 detail.

### Cleanup semantics (extends `SocketManager`)

- `SocketManager` is currently keyed by `container_id` (one server/writer/task
  each). It gains a session dimension: a set of session servers/connections
  per container. This is a real but contained refactor.
- **On detach:** unlink the `{session-id}.sock` file and close its server, but
  the guest keeps the shell/PTY/emulator alive — so a later `session_start`
  re-attach gets a fresh socket plus the snapshot.
- **On terminate:** same socket cleanup, and the guest drops the session.
- **On container stop (resume-able):** `close_socket` keeps the folder and
  `orchestrator.sock` (as today, for resume); it must additionally close and
  unlink all session sockets and consider every session gone — neither active
  nor *detached* sessions survive a stop, because the shell processes and the
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
- **Guest dial failure / race.** If the guest fails to dial after
  `session_start` (e.g. it's mid-restart), how does the orchestrator learn and
  surface the failure to the client? Need a timeout + error path, not a hang.
- **Detached-session lifetime / GC.** A paused session holds a live shell and
  emulator state in the guest with no client attached. What reaps an abandoned
  detached session — a per-session idle timeout, an explicit
  `session_terminate`, or the container's own idle reaper? And does a detached
  session still count as activity for the container idle-timeout reaper?
- **`session_start` vs `session_attach`.** Whether to overload `session_start`
  for both create and re-attach (smaller protocol) or split them (clearer
  intent, lets the guest reject a re-attach to an unknown id explicitly).
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
  authoritative screen model. The emulator is the mechanism that makes
  pause/resume cheap: on (re)attach the guest renders the current screen to a
  **snapshot** frame and sends that, then streams live PTY output — no full
  byte-history replay. `pyte` is a **new external dependency**, which the
  executor has so far avoided (see the executor README's "zero external
  dependencies" claim) — a deliberate trade-off to weigh.
- Apply window size to **both** the PTY (`fcntl.ioctl(fd, termios.TIOCSWINSZ,
  …)`) and the emulator screen (`Screen.resize`) on `resize`, so the snapshot
  stays correctly dimensioned.
- Keep the shell/PTY/emulator alive across detach; tear them down only on
  shell exit or `session_terminate`. Report the shell's exit status.
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
