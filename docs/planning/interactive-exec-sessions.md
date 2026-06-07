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
- **`destroy_socket` does an explicit session sweep.** Today `destroy_socket`
  does `unlink(orchestrator.sock)` then `os.rmdir(container_dir)` (swallowing
  `OSError`); `rmdir` fails on a non-empty directory, so any leftover
  `sessions/{…}.sock` would silently orphan the folder. The decided behaviour:
  before removing the container directory, **iterate the `sessions/` folder, and
  for each session socket mark its row `closed` in the `sessions` table (see
  Database) then unlink the file**. After the sweep, remove the (now empty)
  `sessions/` subdir and `orchestrator.sock`, then `rmdir` the container dir.
  The same sweep runs on container **stop** (mark rows closed + unlink
  sockets), since no session survives a stop — the only difference is stop keeps
  the folder and `orchestrator.sock` for resume. The explicit per-session sweep
  is preferred over a blind `shutil.rmtree` precisely because each session needs
  its DB row reconciled, not just its file deleted.

### Resolved within this plan (Axis 1)

- **Path constant ownership.** The in-container path
  (`/var/run/drover/sockets/`, `orchestrator.sock`, the `sessions/` subdir, and
  `sessions/{session_id}.sock`) is defined as a **specification in the docs**,
  not shared via code or environment variables. The orchestrator enforces it in
  practice by being the side that sets the bind mounts, and the in-container
  relative path never changes, so there is nothing to pass around at runtime.
  This is deliberate: it lets other engineers build their own executor-like
  in-container application against a documented, stable contract rather than
  against our Python `config.py` constants. (The host path stays separate and
  self-discovered — `DROVER_SOCKETS_DIR` / `self._host_socket_dir` — and is not
  part of this in-container contract.)
- **Operator-facing listing endpoint.** Add a per-container
  `GET /containers/{id}/sessions` that lists all sessions for the container,
  newest first, as a snapshot — mirroring `GET /containers/{id}/execs`. Each
  entry comes from the `sessions` row: `id`, `status`, `created_at`,
  `last_client_data_at`, `last_guest_data_at`, `exit_code`, `exit_status`. Keep
  it simple — no filters. (Consistent pagination across all list endpoints is
  already tracked separately in `TODO.md`.)

### Database — new `sessions` table

Sessions get their own table, one row per interactive session, modelled on the
existing `commands` table (ULID `TEXT` PK, FK to `containers(id)`, ISO-8601
`TEXT` timestamps, nullable `exit_code INTEGER`, a `status` column with a
textual default, and a `container_id` index). The two coarse activity
timestamps the spec calls for live here as columns so they survive across the
paused-with-no-client window and are queryable by an operator.

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id                   TEXT PRIMARY KEY,           -- ULID, orchestrator-generated
    container_id         TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'starting',
    created_at           TEXT NOT NULL,              -- ISO 8601
    last_client_data_at  TEXT,                       -- last byte received FROM the client
    last_guest_data_at   TEXT,                       -- last byte sent BY the guest
    exit_code            INTEGER,                    -- NULL until terminal
    exit_status          TEXT,                       -- how it ended (see below); NULL until terminal
    FOREIGN KEY (container_id) REFERENCES containers(id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_container_id
    ON sessions(container_id);
```

Column notes:

- **`id`** — ULID, generated like `command_id` (orchestrator-authoritative,
  lexicographically sortable by creation time). It is the `session_id` carried
  on every control-plane message.
- **`status`** — lifecycle state, mirroring how `containers.status` /
  `commands.status` are textual. Proposed values:
  - `starting` — `session_start` sent, guest has not yet dialed.
  - `running` — guest dialed and the PTY/emulator is live (covers both
    transmitting and PTY-paused; pause/resume is *not* a session-lifecycle
    state and so is deliberately not stored here).
  - `closed` — terminal. The row is retained for audit, like `containers`
    rows after destruction.
- **`last_client_data_at` / `last_guest_data_at`** — the spec's two coarse
  activity timestamps. Updated approximately (not on every byte — e.g. coalesced
  to at most once per few seconds) so they don't become a write hotspot; "idle
  for hours/days" granularity is all that's required.
- **`exit_code`** — the shell's exit code, set from `session_pty_stop` for a
  guest-initiated exit; `NULL` for sessions that never ran a shell to completion
  (e.g. rejected, or orchestrator/operator-terminated).
- **`exit_status`** — *how* the session ended, since `exit_code` alone can't
  distinguish a clean shell exit from a forced teardown. Proposed values:
  `shell_exit` (guest-initiated via `session_pty_stop`), `terminated`
  (orchestrator/operator `session_terminate`), `rejected` (guest sent
  `session_rejected`), `container_stopped` (swept on container stop/destroy).

Lifecycle → table writes:

- **start:** INSERT `status='starting'`, `created_at` set.
- **guest dials:** UPDATE `status='running'`.
- **`session_rejected`:** UPDATE `status='closed'`, `exit_status='rejected'`.
- **data flowing:** UPDATE the relevant `last_*_data_at` (coalesced).
- **`session_pty_stop`:** UPDATE `status='closed'`, `exit_status='shell_exit'`,
  `exit_code=N`.
- **`session_terminate` → `session_terminated`:** UPDATE `status='closed'`,
  `exit_status='terminated'`.
- **container stop/destroy sweep:** UPDATE any non-`closed` row to
  `status='closed'`, `exit_status='container_stopped'`, then unlink the socket.

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

- **Folder cleanup vs. orphaned rows (Axis 1):** `destroy_socket`'s current
  `rmdir` won't remove a populated `sessions/` subtree, and a blind delete would
  leave `sessions` rows stuck non-terminal. Mitigation is the decided explicit
  sweep (mark each row `closed` then unlink, before `rmdir`) — see the
  `SocketManager` and Database notes in Axis 1.
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
- `orchestrator/README.md` — document the new client WS endpoint, the
  `GET /containers/{id}/sessions` listing route (in the API reference table
  alongside the `execs` routes), and the new `sessions` table (the Database
  section lists `containers` / `commands` / `command_messages`).
- `orchestrator/database.py` — add the `sessions` table to `_SCHEMA`.
- `docs/interactive-sessions.md` is the canonical home for the in-container
  socket-path contract that third-party executor implementations build against
  (per the path-ownership decision); keep that path spec authoritative there.
- New ADR(s) once adopted: the transport choice (Axis 1) and the `interactive`
  capability decision are both ADR-worthy.
