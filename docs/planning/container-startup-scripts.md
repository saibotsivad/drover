# Plan: Container Startup Scripts

## Goal / Desired Outcome

A caller launching a micro-container can supply an arbitrary bash **startup
script** that the guest agent runs *once*, during the `initializing` phase,
before the container transitions to `running`. If the script fails, the
container fails to `error` instead of becoming `running`.

Observable outcomes:

- The web UI launch form (`/views/launch`) has a "Startup Script" textarea
  with a "don't put secrets here" warning.
- The container detail page (`/views/containers/<id>`) shows a button that
  toggles the saved script into view, hidden by default.
- The script is persisted in the orchestrator database and returned on the
  container API; **environment variables remain unstored and unprinted**.
- The `drover` CLI gains a flag to supply a script file.
- The script runs **only on first init**, not on resume, and a non-zero exit
  drives the container to `error`.

---

## Background

Relevant prior art:

- ADR [`2026-04-21-ready-message-for-container-init.md`](../decisions/2026-04-21-ready-message-for-container-init.md)
  established the `ready` message: the guest agent connects, runs
  `on_connect()`, then sends `{"type": "ready"}`, and the orchestrator
  transitions `initializing`/`resuming` → `running` only on that message. A
  watchdog fails the container to `error` (`init_timeout`) if `ready` does not
  arrive within `DROVER_INIT_TIMEOUT_SECONDS` (default 20s).
- [`docs/container-initialization.md`](../container-initialization.md)
  documents the init and resume handshakes and the `error_code` table.
- The capabilities plan ([`drover-capabilities-label.md`](./drover-capabilities-label.md))
  is the model for "touch every layer" changes (orchestrator + webapp + docs).

Key facts established by reading the code:

- **No DB migration system.** `orchestrator/database.py` runs a fixed
  `CREATE TABLE IF NOT EXISTS` schema on connect. Adding a column to an
  existing deployment requires an explicit guarded `ALTER TABLE`.
- **`env` is never stored or returned.** It is assembled into the Docker
  `Env` list inside `ContainerManager._init_container` and otherwise lives
  nowhere. `ContainerResponse` has no `env` field. The script, by contrast,
  must be stored and surfaced.
- **The socket protocol is one-directional for control today**: the only
  orchestrator→guest message is `command`. The guest sends
  `ready`/`heartbeat`/`output`/`result`/`done`.
- **`Agent.on_connect()` fires on every connect** — init *and* resume — so it
  is not by itself a safe "first init only" hook.

---

## Problem Statement

Today the only way to do per-container startup work is to bake it into the
image or subclass the executor `Agent` in Python. There is no way for an API
caller to inject a one-off bash script at launch time and have its success
gate the container's readiness.

---

## Proposal

Deliver the script to the guest **over the existing Unix socket** as a new
orchestrator→guest `init` message, run it in the executor before `ready`, and
report failure with a new `init_failed` guest→orchestrator message. The
orchestrator only includes the script in the `init` message during first init,
which gives us "init-only" semantics for free without any guest-side marker
file.

### New handshake

```
init:
  orchestrator: docker start → guest connects
  orchestrator: sends {"type": "init", "script": "<script or null>"}
  guest: on_connect() → if script: run it
           success → {"type": "ready"}
           non-zero exit / error → {"type": "init_failed", "exit_code": N}
  orchestrator: ready → running   |   init_failed → error (init_script_error)

resume:
  orchestrator: sends {"type": "init", "script": null}   (never re-runs)
  guest: on_connect() → {"type": "ready"}
```

The orchestrator always sends exactly one `init` message as the first line on
every accepted connection, so the guest has a single deterministic thing to
wait for. Whether `script` is populated depends on the row's current status
(`initializing` + script present) — that is the entire init-vs-resume
distinction.

---

## Alternatives Considered

1. **Deliver the script as an env var (`DROVER_STARTUP_SCRIPT`).** Simplest —
   reuses the existing `Env` plumbing and needs no protocol change. Rejected
   because env is baked into the Docker container at create time, so it would
   be present (and re-run) on every resume, and "init-only" would then require
   a guest-side marker file. The socket approach lets the *orchestrator* decide
   per-start, which is cleaner. (This was an explicit decision — socket chosen
   over env var.)

2. **Guest-side run-once marker file with env-var delivery.** Works for
   init-only but couples correctness to a sentinel file surviving in the right
   filesystem layer, and still leaks the script to `env` inside the container.
   Rejected in favour of the socket message.

3. **Fail silently / continue on script error.** Rejected — a startup script
   is an init gate; a non-zero exit should produce `error`, not a misleadingly
   `running` container.

---

## Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Transport | Socket `init` message (orchestrator→guest) | Lets the orchestrator decide per-start; no env leakage; init-only without a marker file. |
| Re-run on resume | Never | Orchestrator omits the script from the `init` message unless status is `initializing`. |
| Failure semantics | Non-zero exit → container `error` with `error_code = init_script_error` | Explicit, fast failure via a new `init_failed` message rather than waiting for the 20s watchdog. |
| Storage | New `script TEXT` column on `containers`; returned in `ContainerResponse` | Script is non-secret and must be viewable in the UI; env stays unstored. |
| CLI | New `--script-file` flag on `drover start` | Keep CLI/API/UI in parity. |
| Order vs `on_connect()` | Run `on_connect()` first, then the bash script, then `ready` | Subclass setup is in place before the user script runs. |

---

## Implementation Notes

Ordered to minimise risk; each layer is independently testable.

### 1. Orchestrator — model + storage

- `orchestrator/models.py`
  - `CreateContainerRequest`: add `script: str | None = Field(default=None, max_length=...)`.
    Allow newlines/tabs (it is a shell script); reuse the printable-with-tab/newline
    validator pattern used for `label`, or accept arbitrary text. Pick a max
    length (proposal: 65 536 bytes; note the webapp body-limit interaction below).
  - `ContainerResponse`: add `script: str | None = None`.
- `orchestrator/database.py`
  - Add `script TEXT` to the `containers` schema (for fresh DBs).
  - Add a guarded migration in `connect()` for existing DBs:
    `PRAGMA table_info(containers)` → if `script` absent,
    `ALTER TABLE containers ADD COLUMN script TEXT`.
- `orchestrator/container_manager.py`
  - `create_container`: persist `req.script` in the INSERT (new column).
  - `_row_to_response`: include `script=row["script"]`. (Also returned by
    `list_containers` — harmless; non-secret.)

### 2. Orchestrator — socket protocol

- `orchestrator/socket_manager.py`
  - In `_handle_connection`, after registering the writer and before the read
    loop, fetch `SELECT status, script FROM containers WHERE id = ?` and send
    one `init` message: `{"type": "init", "script": <script if status ==
    'initializing' and script else null>}`.
  - In `_handle_message`, handle a new guest→orchestrator type
    `init_failed` → invoke a new `init_failed` callback with the exit code.
  - Add `set_init_failed_callback`, mirroring `set_ready_callback`.
  - Update the module docstring's message-type lists (add `init` outbound,
    `init_failed` inbound).
- `orchestrator/container_manager.py`
  - Add `fail_container_init(container_id, error_code="init_script_error")`
    that calls the existing `_fail_init(...)` (transition `initializing` →
    `error`, stop logs, destroy socket, force-remove Docker container) and
    cancels/pops the init watchdog (as `on_container_ready` does) for prompt
    failure.
- `orchestrator/app.py`
  - Wire `sockets.set_init_failed_callback(manager.fail_container_init)`
    alongside the existing ready/done callback wiring.

### 3. Executor — run the script before ready

- `executor/drover_executor/protocol.py`
  - Add `encode_init_failed(exit_code)` →
    `{"type": "init_failed", "exit_code": N}`. Update the header docstring.
- `executor/drover_executor/agent.py`
  - In `run()`, after connecting and before sending `ready`:
    1. Read the first line; expect `{"type": "init", ...}`. Extract `script`.
    2. `await self.on_connect()` (unchanged hook).
    3. If `script`: run it via `run_command` (reuse the subprocess runner).
       Stream its stdout/stderr to the guest's own stdout/stderr so it lands in
       Docker logs / log capture (it is **not** an exec command, so it does not
       appear in the commands table).
    4. On exit code `0` → send `ready` (existing path).
       On non-zero (or exception) → send `init_failed` and **do not** send
       `ready`; close the connection.
  - Document the new handshake in the agent/module docstrings.
- `executor/drover_executor/__main__.py`: no change (default `Agent` handles
  it).

### 4. CLI

- `cli/internal/api/types.go`: add `Script string \`json:"script,omitempty"\``
  to the create-request struct; add `Script *string \`json:"script,omitempty"\``
  to the container-response struct.
- `cli/internal/commands/start.go`: add `--script-file` flag; read the file,
  set `req.Script`. (A raw `--script` string flag can be added too, but
  `--script-file` is the primary ergonomic path for multi-line scripts.)

### 5. Webapp

- `webapp/src/views/partials/launch-form.js`: add a "Startup Script" `<textarea
  name="script">` under the Environment field, with a muted warning: *"Do not
  put secrets here — this script is saved in the database. Use environment
  variables for secrets."* Preserve the value on re-render via `values.script`.
- `webapp/src/routes/actions.js`: read `body.script`, include it in the
  create payload when non-empty. **Raise the `urlencoded` body limit** from
  `64kb` (a 64 KB script plus env would otherwise be rejected) to e.g.
  `256kb`, kept consistent with the model's `max_length`.
- `webapp/src/views/partials/container-detail.js`: when `container.script` is
  present, render a button that toggles a `<pre>` of the script (start hidden,
  e.g. inline `onclick` toggling a `hidden` attribute, matching the existing
  no-build inline-handler style). Omit entirely when there is no script.
- `webapp/src/routes/views.js`: no change — the detail route already passes the
  full container object, which now carries `script`.

### 6. Docs + changelog

- `README.md`: add `script` to the Create Request Example and the Request
  Validation table; add `init` (outbound) and `init_failed` (inbound) to the
  Socket Protocol section; add `init_script_error` to the `error_code` tables.
- `docs/container-initialization.md`: document the `init` message, the script
  step, init-only-not-resume behaviour, and the `init_script_error` code.
- `docs/cli.md`: document `--script-file`.
- New ADR in `docs/decisions/` recording the socket-delivery + init-only +
  fail-on-error decision (per `how-to-create-a-plan.md`).
- `changes/`: add a change file bumping `orchestrator` (minor), `executor`
  (minor), `webapp` (minor), `cli` (minor), and `builder` (patch — rebuild to
  ship the new agent).

### 7. Tests

- Orchestrator (`tests/`): `script` validation in `test_models.py`; column +
  migration in `test_database.py`; script persisted and returned, `init`
  message emitted, `init_failed` → `error` with `init_script_error`, and *no*
  script in the resume `init` message in `test_container_manager.py` /
  `test_websockets.py`.
- Executor (`executor/tests/`): `encode_init_failed` in `test_protocol.py`;
  init-message wait, script run, success→ready and failure→init_failed paths
  in `test_agent.py` (against the mock socket server).
- Webapp (`webapp/test/`): textarea renders with warning and round-trips the
  value; payload includes `script`; detail page renders the toggle only when a
  script is present.
- CLI: `--script-file` populates the request (table/start test).

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| **Long scripts exceed `DROVER_INIT_TIMEOUT_SECONDS` (20s)** and get killed as `init_timeout`. | Document it; operators raise the env var. Per-container init timeout is out of scope (see Open Questions). |
| **Executor/orchestrator version skew.** A new agent waits for an `init` message; an old orchestrator never sends one → the agent hangs until `init_timeout`. | Treat this as a coordinated protocol bump (both versions bump together; the builder image bundles the executor). Document the coupling. Optionally bound the wait and proceed to `ready` if no `init` arrives — decide during implementation. |
| **DB migration on existing deployments.** | Guarded `ALTER TABLE ADD COLUMN` is additive and nullable; safe and idempotent. Add a test that connecting to a pre-existing schema adds the column. |
| **Webapp body limit rejects large scripts.** | Raise the `urlencoded` limit in lockstep with the model `max_length`. |
| **Script secrets persisted in the DB.** | Explicit UI warning; env vars remain the secret channel (unstored, unprinted). Documented behaviour, not a bug. |
| **Rollback.** | Additive throughout. Reverting the executor restores the old "ready immediately" agent; the nullable column can stay. No destructive change. |

---

## Open Questions

| Question | Default / recommendation |
|---|---|
| Should `init_failed` carry a captured tail of script output for diagnostics, or just the exit code? | Start with exit code only; script output is already in Docker logs / log capture. |
| Should the init timeout be configurable per-container so long startup scripts don't trip the global watchdog? | Out of scope for this plan; operators raise `DROVER_INIT_TIMEOUT_SECONDS`. Note as a future improvement. |
| Should the new agent bound its wait for the `init` message (to tolerate an old orchestrator), or block indefinitely? | Decide during implementation; bounded wait is safer, indefinite is simpler given the coordinated bump. |
| Max script length and exact webapp body limit. | Proposal: 64 KB script max, 256 KB body limit. Confirm during implementation. |

---

## Documentation Impact

- Update: `README.md`, `docs/container-initialization.md`, `docs/cli.md`.
- Create: ADR in `docs/decisions/` for the startup-script protocol decision.
- This planning document to be marked adopted once reviewed.
