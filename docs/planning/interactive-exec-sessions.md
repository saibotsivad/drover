# Interactive Exec Sessions

**Status:** Draft — Axis 1 (orchestrator↔guest transport) is decided and now
specified in [`docs/interactive-sessions.md`](../interactive-sessions.md); its
per-container socket-folder groundwork has landed in `main`. The per-session
sockets on top of it, plus Axes 2–4, are still to build. This plan defers to
that spec for the lifecycle and covers only the implementation work and the
remaining axes.

## Goal / Desired Outcome

`drover exec <container-id>` (no `-- <command>`) drops the operator into an
interactive shell inside the micro-container: a real PTY, bidirectional
stdin/stdout, terminal resize handling, and an exit code that mirrors the
shell. `drover exec <id> -- <cmd>` keeps working exactly as it does today.

## Background

Read first: the session transport/lifecycle spec
(`docs/interactive-sessions.md`, the source of truth for Axis 1), the exec flow
(`docs/exec-commands.md`), and the WebSockets ADR
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

## Axis 1 — Orchestrator ↔ guest transport — **DECIDED & SPECIFIED**

The transport and full session lifecycle are now specified authoritatively in
**[`docs/interactive-sessions.md`](../interactive-sessions.md)** — two planes
(control plane on the shared `orchestrator.sock`, data plane on a per-session
socket under `sessions/`), the `session_*` message set, pause/resume PTY flow
control, the snapshot-on-resume model, and termination/cleanup. That document
is the source of truth; this plan does not restate it, to avoid drift. What
follows is only the **implementation-level** detail the spec deliberately
omits, plus a record of what the spec resolved.

### Resolved by the spec

- **Snapshot fidelity:** visible screen only; scrollback is not preserved.
- **Start failure/race:** no start timeout — a guest that never dials and never
  sends `session_rejected` just produces no data; the operator ends the session
  and starts a new one.
- **Abandoned (paused) session GC:** nothing auto-reaps a paused session whose
  client never returns. The orchestrator tracks two coarse per-session
  timestamps (last client→guest data, last guest→client data) so an operator
  can find and end stale sessions manually.
- **Concurrency:** no built-in cap on sessions per container; no relationship
  to `--max-concurrent-commands`.
- **Capability:** the key is **`interactive`** (distinct from `exec`).
- **Pause/resume plane:** control plane (the main socket).
- **Stale socket after an orchestrator crash:** ignored; removed when the
  container is destroyed.

### Host-vs-in-container paths (implementation note)

The spec describes the in-container tree. Implementers also need the host side,
which the existing per-container `orchestrator.sock` already establishes:

- **Host path** — `DROVER_SOCKETS_DIR`, self-discovered at startup
  (`self._host_socket_dir` in `container_manager.py`, from the orchestrator's
  own Docker `Mounts`). The bind *source* must be the host path because Docker
  resolves nested bind-mount sources against the host filesystem.
- **In-container path** — fixed `SOCKET_DIR = /var/run/drover/sockets/`, where
  the orchestrator creates sockets and the guest finds them.

Session sockets reuse the exact host-vs-in-container split already used for
`orchestrator.sock`, one level deeper under `sessions/`. The existing
self-inspection already supplies `self._host_socket_dir`, so **no new host-path
discovery is needed**, and because the per-container folder is mounted as a
*directory*, session sockets created after container start appear inside the
container automatically.

### `SocketManager` changes (implementation note)

- `SocketManager` is currently keyed by `container_id` (one server/writer/task
  each). It gains a session dimension: a set of session servers/connections per
  container. A real but contained refactor.
- The orchestrator must create the `sessions/` subdir when starting a session.
- ⚠️ **`destroy_socket` cleanup bug.** To honor the spec's "remove the entire
  `{container_id}/` tree including `sessions/`", note that today
  `destroy_socket` does `unlink(orchestrator.sock)` then `os.rmdir(container_dir)`
  (swallowing `OSError`). `rmdir` fails on a non-empty directory, so any
  leftover `sessions/{…}.sock` would silently orphan the folder. This must
  become a recursive removal (e.g. `shutil.rmtree`) or an explicit `sessions/`
  sweep before the `rmdir`.

### Open questions / unresolved (Axis 1)

- **Path constant ownership.** The in-container path is already a *shared*
  contract: the orchestrator owns `SOCKET_DIR` + `ORCHESTRATOR_SOCKET_NAME`
  (`config.py`), while the executor independently hardcodes the same
  `/var/run/drover/sockets/orchestrator.sock` as its default. Adding `sessions/`
  + the session-socket naming extends that shared contract. Decide where it is
  defined so orchestrator and executor can't drift. (The host path stays
  separate and self-discovered — `DROVER_SOCKETS_DIR` / `self._host_socket_dir`
  — and is not part of this in-container contract.)
- **Per-session timestamp storage.** The spec calls for two coarse per-session
  timestamps for operator visibility. Decide where they live (in-memory on the
  orchestrator vs. a DB row) and whether an operator-facing listing endpoint is
  needed to surface them.

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

## Axis 3 — Executor PTY mechanics (new)

The session *behaviour* (snapshot-on-resume, continuous emulator feeding,
visible-screen-only, pause/resume) is specified in
[`docs/interactive-sessions.md`](../interactive-sessions.md). This axis is the
guest-side implementation of it in `drover-executor`:

- Allocate a PTY (`pty.openpty` / `os.openpty` + `create_subprocess_exec`, or
  `pty.fork`), launch the operator's login shell (`$SHELL` or `/bin/sh`).
- Hold the authoritative screen model in an **in-memory terminal emulator**.
  Candidate: `pyte` (pure-Python VT-compatible `Screen`; its base `Screen` is
  visible-grid-only, which matches the spec's visible-screen-only snapshot). It
  is a **new external dependency**, which the executor has so far avoided (see
  the executor README's "zero external dependencies" claim) — a deliberate
  trade-off to weigh, vs. a vendored/minimal emulator.
- Apply `resize` to **both** the PTY (`fcntl.ioctl(fd, termios.TIOCSWINSZ, …)`)
  and the emulator screen (`Screen.resize`) so snapshots stay correctly
  dimensioned.
- Kill the shell's process group on teardown/cancel (mirroring
  `runner.run_command`'s `killpg`) to avoid orphaned children.
- Expose as an override point on `Agent` (e.g. `on_interactive`) consistent
  with the existing `on_command` hook.

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

The capability key is **`interactive`** (per the spec), gated in
`container_manager` and advertised on interactive-capable images, per
`docs/capabilities.md`'s "Adding a new capability" steps.

## Open Questions

These are the cross-cutting questions the spec does **not** settle (it covers
Axis 1 transport/lifecycle only; capability, concurrency, persistence,
snapshot fidelity, and pause/resume plane are resolved above).

- **PTY output framing (Axis 2):** base64 vs. a binary WS frame type between
  client and orchestrator, to avoid bloating interactive latency.
- **Auth on the client endpoint (Axis 2):** reuse the WS Bearer/`?token=`
  scheme from `websockets.py` verbatim.
- **Idle-timeout reaper interaction:** the spec's coarse per-session timestamps
  give operator *visibility*, but it does not say whether a live (unattached)
  session counts as container activity for the existing idle reaper, which keys
  off guest heartbeats. Confirm an active interactive session can't be reaped
  out from under a connected operator.

## Risks and Mitigations

- **Recursive folder cleanup (Axis 1):** `destroy_socket`'s current `rmdir`
  won't remove a `sessions/` subtree; switch to a recursive removal or sweep
  `sessions/` first. (Per the spec, a crash-leftover session socket is
  otherwise harmless — it's removed when the container is destroyed.)
- **Raw-mode terminal corruption:** always restore terminal state via
  `defer`, including on panic/signal.
- **Leaked PTYs / zombie shells:** kill the process group on
  disconnect/cancel, mirroring `runner.run_command`'s `killpg` on cancel.
- **Capability bypass:** enforce in the orchestrator, not just the CLI/webapp.

## Documentation Impact

- `docs/interactive-sessions.md` — already written (the Axis 1 spec); keep in
  sync if Axes 2–4 change anything in it (e.g. the client-side framing).
- `docs/capabilities.md` — add the `interactive` capability row.
- `docs/cli.md` — document `drover exec <id>` interactive behaviour.
- `executor/README.md` — document the new PTY hook/override.
- `orchestrator/README.md` — document the new client WS endpoint.
- New ADR(s) once adopted: the transport choice (Axis 1) and the `interactive`
  capability decision are both ADR-worthy.
