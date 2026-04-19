# Plan: Container Bootstrap, Auto-Init, and Readiness Protocol

Adds a standardized pre-work initialization phase to every Drover micro-container. Introduces (1) a new readiness handshake between executor and orchestrator, (2) an optional user-supplied startup script, and (3) a bundled auto-init flow that clones a git repository and installs its dependencies.

The goal is that an operator can launch a stock Drover image, set `DROVER_AUTO_GIT_URL`, and have a ready-to-command workspace with zero custom image building.

---

## Motivation

Today a Drover image is either a generic shell runner (and the caller must issue every `mkdir`/`clone`/`pip install` by hand) or a bespoke image (and the operator has to build and tag a new image for each workload). There is no middle ground.

There is also no formal notion of "the container is done booting." The orchestrator flips to `running` the moment Docker starts the container, even though the executor may still be seconds away from accepting commands. Callers work around this by polling or sleeping.

Both gaps are solved by the same change: a multi-step readiness handshake with a well-defined initialization phase.

---

## User-Facing Behavior

### Launching a stock image with auto-init

```http
POST /containers
{
  "image": "python-runner",
  "env": {
    "DROVER_AUTO_GIT_URL": "https://github.com/acme/widgets.git",
    "DROVER_AUTO_GIT_TOKEN": "ghp_...",
    "DROVER_AUTO_GIT_REF": "main"
  },
  "startup_script": "apt-get install -y libpq-dev",
  "timeout_seconds": 600
}
```

The orchestrator launches the container, the executor runs the optional `startup_script`, clones the repo, reads `drover.yaml`, installs dependencies, and signals `ready`. The `POST` response returns when the container reaches `ready` (or fails). Subsequent `POST /exec` calls land in a prepared workspace.

If the cloned repo contains no `drover.yaml`, the executor performs the clone only and goes straight to `ready` — no implicit dependency installation.

### Minimal `drover.yaml`

```yaml
version: 1
workdir: /workspace
setup:
  - pip install -r requirements.txt
  - pip install -e .
env:
  PYTHONUNBUFFERED: "1"
```

---

## Naming

Consistent vocabulary across the orchestrator, executor, and API.

### Container lifecycle states (replaces current `running`)

| State | Meaning |
|---|---|
| `starting` | Docker create+start issued, executor has not yet connected |
| `initializing` | Executor connected and sent `hello`; running `startup_script` and/or auto-init |
| `ready` | Fully initialized, accepting exec commands |
| `stopping` | Existing; transitional |
| `stopped` | Existing; resumable |
| `resuming` | Existing; transitional |
| `destroying` | Existing; transitional |
| `destroyed` | Existing; terminal |

`running` is removed — it was ambiguous (booted vs. prepared). Resuming a stopped container goes `resuming → ready` (skip `initializing`; bootstrap only happens on first start).

### Socket message types

| Direction | Type | Purpose |
|---|---|---|
| E → O | `hello` | Executor is connected; here is its metadata |
| O → E | `init` | Orchestrator's init payload: optional startup script, auto-init config, env overrides |
| E → O | `ready` | Bootstrap complete; ready for commands |
| E → O | `init_failed` | Bootstrap failed; carries reason and optional log excerpt |
| O → E | `command` | Existing |
| E → O | `heartbeat` / `output` / `result` / `done` | Existing |

`hello` replaces the implicit "socket connected" signal. `init` is always sent, even if empty, so the executor has a single synchronization point before it proceeds. `ready` is the new barrier that gates command dispatch.

### Phase names inside the executor

`connecting → hello_sent → awaiting_init → running_startup → running_auto_init → ready`. Used only in executor logs; not exposed over the wire.

---

## Wire Protocol Additions

### `hello` (E → O)

```json
{
  "type": "hello",
  "protocol_version": 1,
  "executor_version": "0.2.0",
  "capabilities": ["auto_init", "startup_script"]
}
```

`capabilities` lets a future minimal custom executor advertise only what it implements (e.g. no `auto_init`). The orchestrator refuses to send an `init` payload that requires a missing capability and fails the container with a clear error.

### `init` (O → E)

```json
{
  "type": "init",
  "startup_script": "apt-get install -y libpq-dev",
  "auto_init": {
    "git_url": "https://github.com/acme/widgets.git",
    "git_ref": "main",
    "git_token": "ghp_...",
    "git_provider": "github",
    "workdir": "/workspace"
  }
}
```

All fields optional. An empty init (`{"type": "init"}`) means "nothing to do, go straight to ready." Secrets (tokens) travel over the Unix socket, not as Docker env vars visible to `docker inspect`. See *Security* below.

### `ready` (E → O)

```json
{
  "type": "ready",
  "workdir": "/workspace",
  "duration_ms": 8421
}
```

Diagnostic metadata; none of it is required by the orchestrator but all of it is useful in logs and returned in `GET /containers/{id}`.

### `init_failed` (E → O)

```json
{
  "type": "init_failed",
  "phase": "running_auto_init",
  "reason": "git clone failed: authentication required",
  "exit_code": 128,
  "log_tail": "..."
}
```

Orchestrator records the reason, transitions the container to `stopping → stopped` (or `destroying → destroyed`, configurable), and surfaces it on `GET /containers/{id}`.

---

## Orchestrator Changes

### State machine

```
[*] → starting            : POST /containers (Docker create+start)
starting → initializing   : hello received
initializing → ready      : ready received
initializing → stopped    : init_failed OR timeout
ready → stopping          : POST /stop, idle timeout, or done signal
... (existing transitions unchanged)
resuming → ready          : Docker confirms start (no re-bootstrap)
```

### `POST /containers` response semantics

Two options; the plan recommends **(B)**:

- **(A) Return immediately** with `status: starting`. Caller polls until `ready`.
- **(B) Block until terminal init outcome** (`ready` or `stopped`) with a bounded wait (default 120s, configurable per-request via `init_timeout_seconds`). Returns `202 Accepted` with `Location` for polling if the wait elapses.

(B) matches how callers actually want to use the API (create-then-exec) while preserving a polling fallback for long bootstraps.

### Init timeout

Separate from the existing idle `timeout_seconds`. Bootstrap often takes longer than the working idle timeout should (think `pip install torch`), but must not be unbounded. Default `init_timeout_seconds = 300`. The reaper treats `starting` and `initializing` as subject to the init timeout (measured from container creation), not the idle timeout.

### New DB columns (migration)

On `containers`:

- `init_started_at TEXT`
- `ready_at TEXT`
- `init_error TEXT` (reason string if `init_failed`)
- `workdir TEXT`

On the request side, `CreateContainerRequest` gains:

- `startup_script: str | None` (max e.g. 64 KB; printable + newline/tab only)
- `init_timeout_seconds: int` (bounds identical to existing timeout)
- `env` already carries `DROVER_AUTO_GIT_*` — nothing new needed there, but tokens passed via env are still supported for non-auto-init images.

### Sending `init`

`SocketManager` gains `send_init(container_id, payload)`. The `ContainerManager` composes the payload from the create request and the detected capabilities from `hello`. It does this in response to `hello`, not speculatively.

### Gating commands

`exec_command` already rejects non-`running` containers; it now rejects anything that is not `ready`. `POST /stop` and `DELETE` remain valid during `starting`/`initializing` so a stuck bootstrap can be aborted.

### Resume

Resume does not re-run bootstrap. On resume the executor is expected to skip straight to `ready` (the existing filesystem already has the workspace). Pseudocode:

```
if DROVER_RESUMED=1:
  send ready
else:
  full bootstrap
```

Orchestrator sets `DROVER_RESUMED=1` when calling Docker `start` on an existing container.

---

## Executor Changes

### New module: `drover_executor/bootstrap.py`

Pure functions + a coordinator. Keeps `agent.py` focused on protocol framing.

- `load_drover_config(repo_dir) -> DroverConfig | None`
  Reads and strictly parses `drover.yaml` at the repo root. Returns `None` if absent.
- `clone_repo(url, ref, token, provider, dest) -> None`
  Wraps `git clone` (and for `radicle` provider, `rad clone`). Token injected via a short-lived `GIT_ASKPASS` helper, never placed in the URL and never written to disk.
- `run_setup(config, send_output) -> int`
  Executes `setup` commands in order. Reuses the existing `runner.run_command` machinery and streams output to a synthetic "init" command ID (see below) so the caller can retrieve bootstrap logs through the normal commands API.

### New agent hook on `Agent`

```python
async def on_init(self, payload: dict) -> None:
    """Override point. Default implementation runs startup_script then auto_init."""
```

Called after `hello`/`init` handshake, before `ready` is sent. Raises `BootstrapError` on failure; agent converts to `init_failed`.

### Init command ID

Bootstrap output is valuable; it is the thing that most often goes wrong. The executor reserves a synthetic command ID (e.g. `__init__`) and wraps `startup_script` and each `setup` line as `output` messages with that ID. Callers can `GET /containers/{id}/exec/__init__` to stream bootstrap logs just like any other command. A single `result` with the final init exit code is sent immediately before `ready` (or in lieu of it on failure).

### `drover.yaml` schema v1

```yaml
version: 1
workdir: /workspace           # optional, default /workspace
env:                          # optional, merged over container env
  PYTHONUNBUFFERED: "1"
setup:                        # optional, shell commands in order
  - pip install -r requirements.txt
  - pip install -e .
after_ready:                  # optional, fire-and-forget warmup (not awaited)
  - python -c "import torch"
```

Unknown top-level keys are rejected, not ignored — keeps future versions honest. `version` is required; v1 is the only version defined.

---

## Git Providers

Unified codepath because GitHub, GitLab, and BitBucket all speak the same HTTPS+token dance. Radicle is separate because it is peer-to-peer and uses `rad`, not `git`.

| `DROVER_AUTO_GIT_PROVIDER` | URL form | Auth |
|---|---|---|
| `github` (default for `github.com`) | `https://github.com/org/repo.git` | `x-access-token:$TOKEN` |
| `gitlab` (default for `gitlab.com` or self-hosted detected by probe) | `https://gitlab.com/org/repo.git` | `oauth2:$TOKEN` |
| `bitbucket` | `https://bitbucket.org/org/repo.git` | `x-token-auth:$TOKEN` |
| `generic` | any `https://...` | Basic with `DROVER_AUTO_GIT_USER` + token, or no auth |
| `radicle` | `rad://<rid>` | Requires `rad` binary in the image; ref via `--ref` |

Provider is auto-inferred from URL host when not set explicitly. Unknown hosts fall back to `generic`.

### Token injection

Tokens are never written into the remote URL (they would leak into `git config` and process listings). The bootstrap sets a one-shot `GIT_ASKPASS` wrapper script in a tmpfs-backed dir, runs the clone with `GIT_TERMINAL_PROMPT=0`, then unlinks the wrapper. Same idea for the `credential.helper` `!` form — chosen for whichever gives cleaner error messages on auth failure.

---

## Security

- **Tokens in `init`, not `env`.** Tokens passed through `env` land in `docker inspect` and in the container process environment for the lifetime of the container. The API accepts them in `env` today for ergonomics, but the orchestrator's init composer moves any `DROVER_AUTO_GIT_TOKEN` out of the Docker env list and into the `init` socket payload before calling `docker create`. The executor receives it over the Unix socket and keeps it in memory only for the duration of the clone. Callers who do not want this behavior can set their token under a different name.
- **Startup script size and content.** Bounded length (64 KB). Printable + `\n`/`\t` only. It runs as root inside the container just like any exec — no new attack surface beyond what `/exec` already grants.
- **Untrusted `drover.yaml`.** The file is inside a cloned repository; its contents are untrusted. The schema is strict (no shell interpolation beyond what the commands themselves invoke). `setup` commands run inside the container, which is already gVisor-sandboxed for non-privileged images. This is the same trust model as any exec command.
- **Host key verification for git.** For `github.com`/`gitlab.com`/`bitbucket.org` the image ships a pinned `known_hosts`. For `generic` HTTPS providers, the container trusts whatever CA bundle it ships (standard). SSH URLs are not supported in v1.

---

## Backward Compatibility

This is a breaking change to the state enum and the socket protocol. Drover has not shipped 1.0 yet and the existing `running` semantics are already wrong, so the plan is to break cleanly rather than maintain a compat mode. Specifically:

- `ContainerStatus.running` is removed from `models.py` and the DB. A one-shot migration rewrites any row with `status='running'` to `status='ready'`.
- Custom executors built against the pre-handshake protocol will fail to reach `ready` (they never send `hello`) and time out with a clear `init timeout: no hello received`. The README documents the required handshake for custom images.

---

## Implementation Checklist

### Protocol / shared

- [ ] Extend `executor/drover_executor/protocol.py` with `encode_hello`, `encode_ready`, `encode_init_failed`, and `decode` handling for `init` / `command`.
- [ ] Mirror the same type names in `orchestrator/socket_manager.py` (which hand-rolls JSON today).
- [ ] Bump `protocol_version` to `1` and document in `docs/decisions/`.

### Orchestrator

- [ ] Add `starting`, `initializing`, `ready` to `ContainerStatus`; remove `running`.
- [ ] DB migration: add `init_started_at`, `ready_at`, `init_error`, `workdir`. Rewrite `running → ready`.
- [ ] `CreateContainerRequest`: add `startup_script`, `init_timeout_seconds`.
- [ ] `SocketManager`: add `send_init`, handlers for `hello`, `ready`, `init_failed`; move token env vars into init payload before Docker create.
- [ ] `ContainerManager`: compose init payload; update state transitions; enforce `ready` gate on exec; separate init timeout in the reaper.
- [ ] `POST /containers`: await `ready`/`init_failed`/`init_timeout` up to `init_timeout_seconds`; return `202` with polling URL on timeout.
- [ ] Resume sets `DROVER_RESUMED=1` and skips bootstrap.

### Executor

- [ ] `bootstrap.py`: `detect_bootstrap`, `clone_repo`, `run_setup`, `parse_drover_yaml` (strict).
- [ ] `agent.py`: `on_init` hook with default implementation; phase tracking; `init_failed` on exception; stream bootstrap output under synthetic `__init__` command ID.
- [ ] Radicle support gated on `rad` presence (skip + clear error if absent in generic image).
- [ ] `GIT_ASKPASS` token handling.
- [ ] Respect `DROVER_RESUMED` — skip to `ready` immediately.

### Tests

- [ ] Protocol round-trip for new message types.
- [ ] Orchestrator: state transitions, init timeout vs idle timeout, `POST /containers` blocking behavior, resume path.
- [ ] Executor: `drover.yaml` parse (happy path + failure cases), missing-file path (clone-only, goes straight to ready), `GIT_ASKPASS` does not leak the token, `init_failed` propagation, `DROVER_RESUMED` fast path.
- [ ] End-to-end (in `test.yml`): build image, create container with `DROVER_AUTO_GIT_URL` pointing at a tiny public fixture repo in this repo's `tests/fixtures/`, assert `ready` and that `requirements.txt` deps are installed.

### Documentation

- [ ] `README.md`: new *Bootstrap and Readiness* section; state diagram update; `drover.yaml` reference.
- [ ] `docs/decisions/2026-04-19-bootstrap-handshake.md`: records the decision to make the handshake mandatory and to break `running`.
- [ ] Update the main `PLAN.md` with a cross-reference to this plan.

---

## Open Questions

1. **Should `POST /containers` block by default?** Recommendation is yes, with a 120s default and override. Alternative: always return immediately and require polling — simpler server, more work for every caller.
2. **Should `after_ready` commands block `ready`?** Current proposal: no, they run async after `ready` is reported. If any caller needs them to block they can move them into `setup`.
3. **Radicle in the base image?** Pulls in ~30 MB. Recommendation: ship a separate `drover/python-runner-radicle` variant rather than bloat the default.
4. **Should the orchestrator accept `auto_init` directly in `POST /containers` (structured) instead of relying on `DROVER_AUTO_GIT_*` env vars?** Structured is cleaner and lets us drop the env-to-init migration for tokens. Proposed: accept both, prefer structured.
