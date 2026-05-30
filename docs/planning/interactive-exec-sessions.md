# Interactive Exec Sessions

**Status:** Draft — Axis 1 (orchestrator↔guest transport) decided; Axes 2–4 still open.

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

Current state (the constraints that shape every option below):

- **One socket file, mounted as a file.** The orchestrator is the Unix-socket
  *server*; the guest agent dials *out* to `/run/orchestrator.sock`. That path
  is a single-file bind mount (`container_manager.py` ~L391:
  `{host_socket_path}:/run/orchestrator.sock`). The container does **not** see
  the socket *folder* — so "a different filename in the same folder" is not
  visible inside the container without a bind-mount change.
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
1:1 with a client. The current stack (single outbound socket, one-way WS,
no-stdin runner) supports none of these directly. The design question is
*where* to add the bidirectional, per-session plumbing.

There are **three independent axes** to decide. None has a single
obviously-correct answer for our stack, so each lists options.

---

## Axis 1 — Orchestrator ↔ guest transport — **DECIDED**

**Decision (team, 2026-05-30): dedicated per-session socket inside a
per-container folder bind mount.** Drover isn't in production use yet, so
backwards compatibility with the current single-file mount is a non-concern,
and gVisor behaviour with a directory mount of Unix sockets has been verified.
The win is clean, independently-managed streams: command traffic and each
interactive session each get their own connection, with no shared framing and
no head-of-line blocking.

### Layout

There are **three distinct path namespaces** here, and PR #139 cleared up a
doc error that conflated them — worth keeping straight in this plan:

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
  per-container thing is mounted. Same for every container.

Today the orchestrator creates one **file** per container at its in-container
path, `/var/run/drover/sockets/{container_id}.sock`, and bind-mounts it —
using the **host** mirror `{host_socket_dir}/{container_id}.sock` as the bind
source — to `/run/orchestrator.sock` in the guest. That changes to one
**folder** per container:

```
orchestrator in-container:  /var/run/drover/sockets/{container_id}/
host (bind source):         {host_socket_dir}/{container_id}/
       (both the same dir via the existing sockets-dir mount)
                              ├── drover.sock        # main orch <-> guest
                              └── sessions/
                                    └── {session-id}.sock   # one per session

guest in-container:         /var/run/drover/sockets/   (same path every container)
                              ├── drover.sock
                              └── sessions/{session-id}.sock
```

- The orchestrator creates the dir tree at its in-container path
  `os.path.join(SOCKET_DIR, container_id)` and bind-mounts the **host** mirror
  `os.path.join(self._host_socket_dir, container_id)` as the source onto the
  guest in-container path `/var/run/drover/sockets/`. This is the exact same
  host-vs-in-container split the single-file mount uses today (`container_id`
  subdir instead of `{container_id}.sock`); the existing self-inspection
  already supplies `self._host_socket_dir`, so **no new host-path discovery is
  needed**.
- The container-side mount path is identical for every container; only the host
  side (and the orchestrator's in-container source dir) is per-container.
- Because it is a **directory** mount (not a file mount), session socket files
  the orchestrator creates *after* container start appear inside the container
  automatically — this is precisely why the folder approach is required and a
  file mount could not work.
- The guest's main socket moves from `/run/orchestrator.sock` to
  `/var/run/drover/sockets/drover.sock`.
- The existing pre-create-before-start invariant inverts: today the file is
  pre-created so the bind target is "a file rather than a directory"
  (`container_manager.py` comment ~L370); now the per-container dir **and**
  `drover.sock` must exist before Docker start so the bind target is a
  populated directory the guest can immediately connect into.

### Roles and ordering

The orchestrator stays the Unix-socket **server** for every socket; the guest
stays the **dialer** — consistent with today's model and unchanged for the
main socket. For an interactive session:

1. Orchestrator creates the session socket server at
   `…/{container_id}/sessions/{session-id}.sock` (`start_unix_server`,
   `chmod` it) **before** announcing it, so it is listening when the guest
   dials.
2. Orchestrator sends a `session_start` lifecycle message over `drover.sock`.
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

### Lifecycle protocol (over the main `drover.sock`)

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
- **On container stop (resume-able):** keep `drover.sock` (as today, for
  resume); close and unlink all session sockets and consider every session
  gone — neither active nor *detached* sessions survive a stop, because the
  shell processes and the in-guest emulator state die with the container.
- **On destroy:** remove the whole `{container_id}/` directory tree.

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
- **Resume + reconnect.** After a stop/resume the guest re-dials `drover.sock`;
  confirm no session state is assumed to survive and that the orchestrator
  rejects/cleans session sockets created before the stop.
- **Session id authority.** Orchestrator-generated (like `command_id`) — but
  confirm whether sessions get a DB row for listing/audit or stay purely
  ephemeral (see global Open Questions).
- **Concurrency caps.** Whether `--max-concurrent-commands` applies to
  sessions, or sessions get their own cap, and what the guest does when over
  the limit (refuse the `session_start`?).
- **Folder bind mount + non-Drover images.** Custom-agent images now mount a
  folder, not a file, and must connect to `…/drover.sock`. Confirm the
  executor default path change is the only client-visible break and document
  it loudly given the no-backwards-compat stance.
- **Path constant ownership.** `/var/run/drover/sockets/` is today the
  orchestrator's own in-container constant (`SOCKET_DIR` in `config.py`); the
  guest currently only knows `/run/orchestrator.sock`. After this change the
  in-container mount path becomes a *shared* contract — the executor must
  hardcode the same `/var/run/drover/sockets/` mount root (plus `drover.sock`
  and `sessions/`). Decide where that contract is defined so orchestrator and
  executor can't drift. (The host path stays separate and self-discovered —
  `DROVER_SOCKETS_DIR` / `self._host_socket_dir` — and is not part of this
  shared in-container contract.)

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
  byte-history replay. `pyte` is pure stdlib-compatible Python, but it is a
  **new external dependency**, which the executor has so far avoided (see the
  executor README's "zero external dependencies" claim) — a deliberate
  trade-off to weigh.
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

- **Axis 1: decided** — dedicated per-session sockets in a per-container folder
  bind mount (see Axis 1 above). Backwards compat is a non-concern and gVisor
  is verified, so the stream-isolation win is worth the mount-convention
  change.
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

- **Folder bind-mount change (Axis 1):** moving from a file mount to a
  per-container directory mount changes the in-container socket path and the
  mount convention. gVisor `--host-uds=all` behaviour with the directory mount
  has been verified by the team; remaining risk is the executor default-path
  break (call it out loudly — no backwards compat) and stale session sockets
  lingering in `sessions/` (sweep on start/stop/destroy).
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
