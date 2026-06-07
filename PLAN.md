# Interactive Exec Sessions — Implementation Plan

Concrete, phased build-out of the design in
[`docs/planning/interactive-exec-sessions.md`](docs/planning/interactive-exec-sessions.md)
(decisions) and [`docs/interactive-sessions.md`](docs/interactive-sessions.md)
(lifecycle spec, source of truth). This file tracks *work*, not design — see
those docs for rationale. Check boxes as phases land.

## Goal

`drover exec <id>` (no `--`) → interactive PTY shell in the micro-container:
bidirectional stdin/stdout, resize, exit code mirrors the shell. `drover exec
<id> -- <cmd>` unchanged.

## Dependency map

```
P1 ─┬─> P2 ─┬─> P3 ─┐
    │       └─> P4 ─┼─> P7(e2e) ─> P8
    └─> P5          │
P6 ────────────────┘
```

- **P1** blocks P2, P3, P5 (DB rows + capability).
- **P2** blocks P3 (REST needs session API) and P4 (WS needs data-plane bridge).
- **P5** needs only P1; runs parallel to P3/P4.
- **P6** (executor) depends only on the protocol/socket contract in the spec →
  **fully parallel** with P1–P5 from day one.
- **P7** (CLI) code is writable immediately; **end-to-end** validation needs
  P3+P4+P6.
- **Parallel tracks:** {P6} ‖ {P1→P2→(P3,P4,P5)} ‖ {P7 code}.

## Shared contracts (freeze before parallel work)

These are already specified; treat as frozen so P6/P7 can build against them.

- **In-container socket path:** `/var/run/drover/sockets/sessions/{session_id}.sock`
  (spec §Socket layout). Documented contract, not shared code.
- **Control-plane messages:** `session_start` / `session_pty_pause` /
  `session_pty_resume` / `session_terminate` (orch→guest); `session_rejected` /
  `session_terminated` / `session_pty_stop` (guest→orch). All carry
  `session_id` (spec §Control-plane messages).
- **WS framing:** PTY bytes = raw **binary** frames both directions; control
  (e.g. resize) = **JSON text** `{"type":"resize","cols":N,"rows":M}`. Snapshot
  rides the binary path.

---

## Phase 1 — Data & capability foundation `[orchestrator]`

Blocks: P2, P3, P5. Depends on: none.

- [ ] Add `sessions` table to `orchestrator/database.py` `_SCHEMA` (id ULID PK,
      `container_id` FK, `status` default `starting`, `created_at`,
      `last_client_data_at`, `last_guest_data_at`, `exit_code`, `exit_status`)
      + `idx_sessions_container_id`.
- [ ] Pydantic model(s) for a session row in `orchestrator/models.py`.
- [ ] Register `interactive` capability: gate via `container_manager._assert_capability`
      (per `docs/capabilities.md` "Adding a new capability" steps), distinct
      from `exec`.
- [ ] Docs: add the `interactive` row to `docs/capabilities.md`; add `sessions`
      table to the `orchestrator/README.md` Database section.

## Phase 2 — SocketManager session plane `[orchestrator, Axis 1]`

Blocks: P3, P4. Depends on: P1.

- [ ] Add session dimension to `SocketManager` (per-container set of session
      servers/connections alongside the single control server) — keyed
      refactor, see planning doc §SocketManager changes.
- [ ] `start_session(container_id, session_id)`: create `sessions/` subdir,
      create+listen on `sessions/{session_id}.sock` **before** sending
      `session_start` on the control plane; INSERT row `status='starting'`.
- [ ] Route inbound control-plane `session_*` messages (extend
      `_handle_message`): `session_rejected` → `closed`/`rejected`;
      `session_terminated` → ack unblock + unlink; `session_pty_stop` →
      `closed`/`shell_exit`/`exit_code`, unlink + close client.
- [ ] Guest-dial detection → UPDATE `status='running'`.
- [ ] Data-plane byte bridge hooks: expose attach/detach so P4 can pump bytes
      verbatim between the session socket and a WS (orchestrator never parses).
- [ ] Coalesced activity-timestamp writes (`last_client_data_at` /
      `last_guest_data_at`, ≤ once per few seconds — not per byte).
- [ ] `terminate_session(...)`: send `session_terminate`, await
      `session_terminated`, then unlink + mark `closed`/`terminated`.
- [ ] Replace blind `rmdir` with explicit **session sweep** in `destroy_socket`
      AND on container stop: iterate `sessions/`, mark each non-`closed` row
      `closed`/`container_stopped`, unlink socket; stop keeps folder +
      `orchestrator.sock`, destroy removes the whole tree.
- [ ] Docs: keep `docs/interactive-sessions.md` in sync if any
      implementation detail diverges from the spec.

## Phase 3 — Session REST endpoints `[orchestrator, Axis 2 control]`

Blocks: P7 (e2e). Depends on: P2.

- [ ] `POST /containers/{id}/sessions` — **only** place the `interactive`
      capability gate runs (`422`). Generate ULID, create+listen, INSERT,
      `session_start`, return `201 {"session_id"}`. `404` unknown container,
      `409` not `running`. Empty body.
- [ ] `GET /containers/{id}/sessions` — list rows, newest first, snapshot, no
      filters (mirrors `…/execs`).
- [ ] `GET /containers/{id}/sessions/{session_id}` — return the row verbatim;
      works for `closed` rows (audit); `404` if absent.
- [ ] `DELETE /containers/{id}/sessions/{session_id}` — terminate via P2;
      idempotent `200/204` on already-`closed`.
- [ ] REST auth via standard `auth_middleware` on all three plain routes.
- [ ] Docs: add the route set to the `orchestrator/README.md` API table next to
      `execs`.

## Phase 4 — Session WS attach / data plane `[orchestrator, Axis 2 data]`

Blocks: P7 (e2e). Depends on: P2.

- [ ] `GET /containers/{id}/sessions/{session_id}/ws` — bidirectional bridge to
      the session socket data plane (binary PTY both ways, JSON control
      client→guest).
- [ ] On WS connect → `session_pty_resume`; on WS disconnect →
      `session_pty_pause`.
- [ ] Single-writer: reject a second concurrent attach with `1008`.
- [ ] In-handler WS auth — reuse the exact `websockets.py` scheme
      (`Authorization: Bearer`, `?token=` fallback, constant-time
      `hash_api_key`, close `1008` on failure).
- [ ] Frame demux: binary ⇒ forward verbatim to guest/client; text ⇒ parse JSON
      control (`resize`, extensible by `type`).
- [ ] Docs: note the attach/pause/resume framing in
      `orchestrator/README.md` (and sync the spec's client-framing section).

## Phase 5 — Idle-reaper exemption `[orchestrator, cross-cutting]`

Depends on: P1. Parallel to: P3, P4.

- [ ] Reaper in `container_manager.py` skips any container with a non-`closed`
      `sessions` row (decouple from WS-attachment — unattached-but-running
      sessions still pin the container).
- [ ] Docs: note the exemption in the `orchestrator/README.md` reaper/lifecycle
      section.

## Phase 6 — Executor PTY mechanics `[executor, Axis 3]`

Depends on: shared contracts only → **parallel with P1–P5**. Feeds: P7 e2e.

- [ ] Add `pyte` dependency (documented exception to executor's zero-dep posture;
      record the revisit trigger in `executor/README.md`).
- [ ] Allocate PTY (`pty.openpty`/`pty.fork`), launch login shell (`$SHELL` →
      `/bin/sh`).
- [ ] Continuously feed PTY output into a `pyte.Screen` (visible-grid only =
      spec's snapshot fidelity); keep screen current even while paused.
- [ ] Data plane: on (re)transmit send full snapshot then live output; consume
      `stdin` + `resize` from client.
- [ ] `resize` → apply to **both** PTY (`TIOCSWINSZ`) and `Screen.resize`.
- [ ] Control-plane handling: dial-on-`session_start` (or `session_rejected`),
      `session_pty_pause`/`resume`, `session_terminate`→`session_terminated`,
      emit `session_pty_stop` with exit code on shell exit.
- [ ] Teardown: `killpg` the shell's process group (mirror `runner.run_command`).
- [ ] Expose `Agent.on_interactive` override hook (consistent with `on_command`).
- [ ] Docs: document the PTY hook/override in `executor/README.md`.

## Phase 7 — CLI terminal handling `[cli/Go, Axis 4]`

Code parallel from start; e2e needs P3+P4+P6.

- [ ] Replace the `interactive_exec_unsupported` stub in
      `cli/internal/commands/exec.go` (no-`--` path).
- [ ] Local terminal raw mode via `golang.org/x/term`; restore on exit via
      `defer` (incl. panic/signal).
- [ ] Dial the bidirectional WS; copy stdin→WS, WS→stdout.
- [ ] Send `resize` on startup and on `SIGWINCH`.
- [ ] Exit with the shell's exit code.
- [ ] Non-TTY stdin (piped) → refuse / graceful fallback.
- [ ] Docs: document interactive `drover exec <id>` behaviour in `docs/cli.md`.

## Phase 8 — Integration, ADRs & doc audit

Depends on: P3, P4, P6, P7.

- [ ] End-to-end test under `e2e/`: interactive-capable image → start container →
      `drover exec <id>` → run shell command → resize → disconnect/reconnect
      (snapshot-on-resume) → shell exit → CLI exits with shell's code.
- [ ] Verify reaper exemption end-to-end: detached running session keeps the
      container alive; container reapable once all sessions terminal.
- [ ] ADR: Axis 1 transport choice (per-session socket / two-plane model).
- [ ] ADR: `interactive` capability decision.
- [ ] **Final doc audit:** re-read every touched doc end-to-end and confirm it
      matches the shipped system — `docs/interactive-sessions.md`,
      `docs/capabilities.md`, `docs/cli.md`, `executor/README.md`,
      `orchestrator/README.md`, and this plan's parent planning doc.

---

## Risks (carried from the planning doc)

- **Abandoned sessions pin containers** (accepted cost of reaper exemption).
  Mitigation is UX, not code: surface stale sessions via the list endpoint +
  activity timestamps. Out of scope here.
- **Folder cleanup vs orphaned rows** → handled by the explicit sweep (P2).
- **Raw-mode terminal corruption** → `defer` restore on all exits (P7).
- **Leaked PTYs / zombie shells** → `killpg` on teardown (P6).
- **Capability bypass** → enforced in orchestrator, not just CLI/webapp (P1/P3).

## Out of scope (recorded extensions)

- `POST …/sessions/{id}/signal` for detached signal-to-shell (terminate stays
  signal-free in v1).
- Concurrent-attach "take over" semantics (single-writer only for now).
- Pagination on list endpoints (tracked separately in `TODO.md`).
- Scrollback in snapshots (visible screen only, per spec).
