# goal

support git clone at startup, before `ready` signal

# short version

if you provide a git url as an env var it'll do a git clone before ready

# non-goals

- **Radicle** — Radicle uses a different CLI (`rad clone`) and a node-based identity model that needs its own design. tracked in a separate draft RFC.
- **git submodules** — complicates per-submodule credential passing. a `DROVER_AUTO_GIT_FLAGS` escape hatch can be added later if there's demand.
- **per-container init timeout override** — the global `DROVER_INIT_TIMEOUT_SECONDS` is the only knob for now. a per-container override in `POST /containers` is a reasonable future addition.

---

# environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DROVER_AUTO_GIT_URL` | yes (to enable) | _(unset)_ | URL of the repository to clone |
| `DROVER_AUTO_GIT_REF` | no | repo default branch | Branch name, tag, or full commit SHA to check out |
| `DROVER_AUTO_GIT_DEPTH` | no | `1` | Shallow clone depth; set to `0` to clone full history |
| `DROVER_AUTO_GIT_TOKEN` | no | _(unset)_ | Bearer token for HTTPS authentication (GitHub, GitLab, Gitea, etc.) |
| `DROVER_AUTO_GIT_SSH_KEY` | no | _(unset)_ | Base64-encoded PEM private key for SSH authentication |

these are passed to the micro-container via the `env` dict in `POST /containers`, the same as any other container environment variable. the orchestrator does not inspect or act on them; they're forwarded to Docker and read by the executor at startup.

---

# clone destination

the repo is always cloned to `/workspace`. this is fixed and not configurable in v1 — a consistent path lets subsequent commands be written portably without knowing the repo name. `/workspace` is short, obvious, and avoids collisions with system paths. after the clone, the executor sets this as the default working directory for all subsequent commands it runs.

---

# authentication

## HTTPS with token

set `DROVER_AUTO_GIT_TOKEN`. the executor rewrites the URL to embed credentials using git's credential-helper protocol:

```sh
git -c credential.helper='!f() { echo "username=x-token"; echo "password=$TOKEN"; }; f' clone <url> /workspace
```

this works for GitHub (personal access tokens and fine-grained tokens), GitLab, Gitea, Forgejo, and Bitbucket. the token is never written to disk. the `-c` flag scopes the credential helper to this invocation only.

the URL in `DROVER_AUTO_GIT_URL` should be a plain HTTPS URL (e.g. `https://github.com/org/repo`), not an already-embedded `https://token@host/...` form — that form leaks the token into log lines.

## SSH

set `DROVER_AUTO_GIT_SSH_KEY` to a base64-encoded PEM private key (the kind you'd normally store in `~/.ssh/id_ed25519`). the executor writes the decoded key to a temporary file under `/tmp`, sets `GIT_SSH_COMMAND` to use it with strict host checking disabled, clones, then shreds the file:

```sh
install -m 600 /dev/null /tmp/drover_id
base64 -d <<< "$KEY" > /tmp/drover_id
GIT_SSH_COMMAND='ssh -i /tmp/drover_id -o StrictHostKeyChecking=no' git clone <url> /workspace
shred -u /tmp/drover_id
```

`StrictHostKeyChecking=no` is intentional for ephemeral containers — there's no host-key database to trust against, and adding a known-hosts bootstrap step would be more friction than the threat model warrants.

## unauthenticated (public repos)

if neither `DROVER_AUTO_GIT_TOKEN` nor `DROVER_AUTO_GIT_SSH_KEY` is set, the executor runs a plain `git clone`. this works for any public repository over HTTPS.

## priority

if both token and SSH key are provided, SSH key wins and a warning is logged.

---

# ref checkout

after cloning, if `DROVER_AUTO_GIT_REF` is set, the executor runs:

```sh
git -C /workspace checkout <ref>
```

this handles branches, tags, and commit SHAs uniformly. if the ref does not exist, the checkout fails, `on_connect` raises, and `ready` is never sent.

if `DROVER_AUTO_GIT_REF` is not set, the clone lands on whatever the remote's HEAD points to (almost always the default branch).

---

# failure handling

all clone steps run inside `on_connect()`. if any step fails — network error, auth failure, bad ref, non-zero exit from git — `on_connect()` raises an exception. the agent then skips sending `ready`, and the orchestrator's init timeout watchdog marks the container as `error` with `error_code: git_clone_failed`.

`git_clone_failed` is a new value added to the `error_code` enum. it sits alongside the existing timeout path so callers can distinguish "clone failed" (possibly worth retrying with different credentials) from "container timed out" (network or image issue).

the executor logs the exact git command (with token redacted) and its stderr output before raising, so operators can diagnose the failure from container logs.

---

# orchestrator considerations

## init timeout

the global `DROVER_INIT_TIMEOUT_SECONDS` (default 20 seconds) must be long enough to cover the clone. 20 seconds is tight for anything but tiny repos on fast links. operators using `DROVER_AUTO_GIT_URL` should raise this to at least 60–120 seconds.

## secrets and env var visibility

env vars passed in `POST /containers` are forwarded verbatim to Docker and never written to the `containers` table. `DROVER_AUTO_GIT_TOKEN` and `DROVER_AUTO_GIT_SSH_KEY` therefore never touch the database — they live only in Docker's memory for the container's lifetime. operators should avoid logging full create request bodies when they contain secrets, and should prefer short-lived tokens where possible.

---

# wire protocol changes

extend the `ready` message with git context when a clone happened:

```json
{
  "type": "ready",
  "workdir": "/workspace",
  "git_cloned": true,
  "git_url": "https://github.com/org/repo",
  "git_ref": "main",
  "git_sha": "a89a33e4f8b1c...",
  "duration_ms": 4210
}
```

fields:
- `workdir` — always `/workspace` when `git_cloned` is true; included so callers don't need to hardcode it
- `git_cloned` — `true` if a clone ran, absent if not
- `git_url` — echoed back with any embedded token redacted (replaced with `***`)
- `git_ref` — the resolved ref name (from `DROVER_AUTO_GIT_REF`, or the detected default branch if unset)
- `git_sha` — the full commit SHA of HEAD after checkout, from `git rev-parse HEAD`
- `duration_ms` — wall-clock milliseconds the clone and checkout took

on the orchestrator side: add `git_sha TEXT` and `git_ref TEXT` to the `containers` table, populated from the ready message. surface both on `GET /containers/{id}`.

---

# executor implementation

the feature lives in a new module `drover_executor/git_clone.py` rather than in `agent.py` directly. this keeps the agent class clean and makes the logic testable in isolation.

```
drover_executor/
  git_clone.py       ← new
  agent.py           ← calls git_clone.run_git_clone() from on_connect()
```

`git_clone.py` exports one async function:

```python
async def run_git_clone(env: dict[str, str]) -> GitCloneResult | None:
    """
    Read DROVER_AUTO_GIT_* from env.
    Return None if DROVER_AUTO_GIT_URL is not set.
    Return GitCloneResult on success.
    Raise RuntimeError on failure (clone or checkout failed).
    """
```

the base `Agent.on_connect()` is modified to call this:

```python
async def on_connect(self) -> None:
    import os
    from drover_executor.git_clone import run_git_clone
    self._git_clone_result = await run_git_clone(os.environ)
```

subclasses that override `on_connect()` and want the git clone behavior should call `await super().on_connect()` first. the result is stored on `self._git_clone_result` so subclasses can inspect the resolved SHA or ref.

the ready message encoding is updated to include the git fields when `self._git_clone_result` is set.

---

# ADR

**Date:** 2026-04-21
**Status:** proposed

## context

Drover micro-containers are ephemeral. today there is no built-in way to get a code checkout into a container before it signals `ready`. operators work around this by baking repos into images (slow iteration) or running clone commands after `ready` (means the container is "ready" before it's actually useful). we want git clone to be a first-class startup step.

## decisions

**fixed clone destination (`/workspace`)**
we considered making the path configurable via `DROVER_AUTO_GIT_DIR`. we rejected it for v1 because a fixed path makes shell commands written against Drover containers portable across repos — you don't need to know the repo name or parameterize it. if a real use case emerges (e.g. monorepo subdirectory workflows), a future RFC can add the variable.

**`git_clone_failed` error code**
the existing `error_code` column had no value for clone failures — they were indistinguishable from init timeouts. adding `git_clone_failed` lets callers make a useful decision: a clone failure is often worth retrying (wrong credentials, transient network), while a timeout usually isn't. we kept the enum narrow rather than adding a general error taxonomy; that's a separate concern.

**no submodule support in v1**
`--recurse-submodules` is one flag, but each submodule URL may need independent credentials, and our current auth model is per-clone, not per-URL. supporting submodules properly would require either a more complex credential scheme or accepting that submodule auth will silently fall back to anonymous. we chose to exclude it and noted `DROVER_AUTO_GIT_FLAGS` as a future escape hatch if operators need it badly enough to pass raw flags themselves.

**Radicle deferred**
Radicle uses a different CLI (`rad clone`), a different identity model (node-managed, not per-clone credentials), and likely requires a running node process in the container image. designing this well requires understanding what a Radicle-ready base image looks like. it is tracked in a separate draft RFC rather than stretching this one.

## consequences

- the `error_code` column gets a new enum value: `git_clone_failed`
- the `containers` table gets two new columns: `git_ref TEXT`, `git_sha TEXT`
- the `ready` wire message gains optional git fields
- `Agent.on_connect()` gains a side effect (reads env, may run git); subclasses calling `super().on_connect()` inherit this behavior automatically
- operators using this feature need to increase `DROVER_INIT_TIMEOUT_SECONDS` beyond the 20-second default

## related

- @docs/rfc/2026-04-22-better-startup-flow.md — drover.yaml setup commands that run after the clone
- @docs/rfc/TODO-radicle-git-clone.md — Radicle clone support (deferred)
- @docs/decisions/2026-04-21-ready-message-for-container-init.md — the ready signal design this feature builds on
