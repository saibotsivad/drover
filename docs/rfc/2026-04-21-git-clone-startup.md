# Goal

Update the Drover executor startup script to support git clone at startup, before `ready` signal.

# Summary

Provide a git URL as a specially named environment variable to have Drover do a git clone before the mini-container sends its readiness signal.

This is a functionality added to the executor library, it is **not** required functionally for a Drover mini-container.

# Non-Goals

- **Non-git code checkout** — This RFC is strictly for git over http or ssh
- **git submodules** — Adds complexity and can happen later
- **Per-container init timeout override** — The global `DROVER_INIT_TIMEOUT_SECONDS` is the only knob for now

---

# Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DROVER_AUTO_GIT_URL` | To enable feature | _(unset)_ | URL of the repository to clone |
| `DROVER_AUTO_GIT_REF` | no | Repo default branch | Branch name, tag, or full commit SHA to check out |
| `DROVER_AUTO_GIT_DEPTH` | no | `1` | Shallow clone depth; set to `0` to clone full history |
| `DROVER_AUTO_GIT_TOKEN` | no | _(unset)_ | Bearer token for HTTPS authentication (GitHub, GitLab, Gitea, etc.) |
| `DROVER_AUTO_GIT_SSH_KEY` | no | _(unset)_ | Base64-encoded PEM private key for SSH authentication |

These are passed to the micro-container via the `env` dict in `POST /containers`, the same as any other container environment variable. The orchestrator does not inspect or act on them; they're forwarded to Docker and read by the executor at startup.

---

# Clone Destination

The repo is always cloned to `/workspace`, this is fixed and not configurable. A consistent path lets subsequent commands be written portably without knowing the repo name. The directory name `/workspace` is short, obvious, and avoids collisions with system paths. After the clone, the executor sets this as the default working directory for all subsequent commands it runs.

---

# Authentication

## HTTPS with token

Set `DROVER_AUTO_GIT_TOKEN`. The executor rewrites the URL to embed credentials using git's credential-helper protocol:

```sh
git -c credential.helper='!f() { echo "username=x-token"; echo "password=$TOKEN"; }; f' clone <url> /workspace
```

This works for GitHub (personal access tokens and fine-grained tokens), GitLab, Gitea, Forgejo, and Bitbucket.

The token is never written to disk, the `-c` flag scopes the credential helper to this invocation only.

The URL in `DROVER_AUTO_GIT_URL` should be a plain HTTPS URL (e.g. `https://github.com/org/repo`), not an already-embedded `https://token@host/...` form as that form will leak the token into log lines.

## SSH

Set `DROVER_AUTO_GIT_SSH_KEY` to a base64-encoded PEM private key (the kind you'd normally store in `~/.ssh/id_ed25519`). The executor writes the decoded key to a temporary file under `/tmp`, sets `GIT_SSH_COMMAND` to use it with strict host checking disabled, clones, then shreds the file:

```sh
install -m 600 /dev/null /tmp/drover_id
base64 -d <<< "$KEY" > /tmp/drover_id
GIT_SSH_COMMAND='ssh -i /tmp/drover_id -o StrictHostKeyChecking=no' git clone <url> /workspace
shred -u /tmp/drover_id
```

Setting `StrictHostKeyChecking=no` is intentional for ephemeral containers — there's no host-key database to trust against, and adding a known-hosts bootstrap step would be more friction than the threat model warrants.

## Unauthenticated (public repos)

If neither `DROVER_AUTO_GIT_TOKEN` nor `DROVER_AUTO_GIT_SSH_KEY` is set, the executor runs a plain `git clone`. This works for any public repository over HTTPS.

## Priority

If both token and SSH key are provided, SSH key wins and a warning is logged.

---

# Ref Checkout

After cloning, if `DROVER_AUTO_GIT_REF` is set, the executor runs:

```sh
git -C /workspace checkout <ref>
```

This handles branches, tags, and commit SHAs uniformly. If the ref does not exist, the checkout fails, `on_connect` raises, and `ready` is never sent.

If `DROVER_AUTO_GIT_REF` is not set, the clone lands on whatever the remote's HEAD points to (almost always the default branch).

---

# Failure Handling

All clone steps run inside `on_connect()`. If any step fails — network error, auth failure, bad ref, non-zero exit from git — `on_connect()` raises an exception. The agent then skips sending `ready`, and the orchestrator's init timeout watchdog marks the container as `error` with `error_code: git_clone_failed`.

We will add `git_clone_failed` as a new value to the `error_code` enum. It sits alongside the existing timeout path so callers can distinguish "clone failed" (possibly worth retrying with different credentials) from "container timed out" (network or image issue).

The executor logs the exact git command (with token redacted) and its stderr output before raising, so operators can diagnose the failure from container logs.

---

# Orchestrator Considerations

## Init Timeout

The global `DROVER_INIT_TIMEOUT_SECONDS` (default 20 seconds) must be long enough to cover the clone, and 20 seconds is tight for anything but tiny repos on fast links. Operators using `DROVER_AUTO_GIT_URL` should raise this to at least 60–120 seconds.

## Secrets

Environment variables passed in `POST /containers` are forwarded verbatim to Docker and never written to the `containers` table. Therefore both `DROVER_AUTO_GIT_TOKEN` and `DROVER_AUTO_GIT_SSH_KEY` never touch the database — they live only in Docker's memory for the container's lifetime.

Operators should avoid logging full create request bodies when they contain secrets, and should prefer short-lived tokens where possible.

---

# Wire Protocol Changes

Extend the `ready` message with git context when a clone happened:

```json
{
  "type": "ready",
  "workdir": "/workspace",
  "git_ref": "main",
  "git_sha": "a89a33e4f8b1c...",
  "duration_ms": 4210
}
```

Fields:
- `workdir` — Always `/workspace` and included so callers don't need to hardcode it
- `git_ref` — The resolved ref name, either from `DROVER_AUTO_GIT_REF` or the detected default branch if unset
- `git_sha` — The full commit SHA of HEAD after checkout, from `git rev-parse HEAD`
- `duration_ms` — Wall-clock milliseconds for the clone and checkout to complete

On the orchestrator side: add `git_sha TEXT` and `git_ref TEXT` to the `containers` table, populated from the ready message. Surface both on `GET /containers/{id}`.

---

# Executor Implementation

The feature lives in a new module `drover_executor/git_clone.py` rather than in `agent.py` directly. This keeps the agent class clean and makes the logic testable in isolation.

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

The base `Agent.on_connect()` is modified to call this:

```python
async def on_connect(self) -> None:
    import os
    from drover_executor.git_clone import run_git_clone
    self._git_clone_result = await run_git_clone(os.environ)
```

Subclasses that override `on_connect()` and want the git clone behavior should call `await super().on_connect()` first. The result is stored on `self._git_clone_result` so subclasses can inspect the resolved SHA or ref.

The ready message encoding is updated to include the git fields when `self._git_clone_result` is set.

---

# ADR

**Date:** 2026-04-21
**Status:** proposed

## context

Drover micro-containers are ephemeral. Today there is no built-in way to get a code checkout into a container before it signals `ready`. Operators work around this by baking repos into images (slow iteration) or running clone commands after `ready` (means the container is "ready" before it's actually useful). We want git clone to be a first-class startup step.

## Decisions

**Fixed clone destination (`/workspace`)**

We considered making the path configurable via `DROVER_AUTO_GIT_DIR` but we rejected it for v1 because a fixed path makes shell commands written against Drover containers portable across repos — you don't need to know the repo name or parameterize it. If a real use case emerges (e.g. monorepo subdirectory workflows), a future RFC can add the variable.

**`git_clone_failed` error code**

The existing `error_code` column had no value for clone failures — they were indistinguishable from init timeouts. Adding `git_clone_failed` lets callers make a useful decision: a clone failure is often worth retrying (wrong credentials, transient network), while a timeout usually isn't. We kept the enum narrow rather than adding a general error taxonomy; that's a separate concern.

**No submodule support in v1**

Although `--recurse-submodules` is one flag, each submodule URL may need independent credentials, and our current auth model is per-clone, not per-URL. Supporting submodules properly would require either a more complex credential scheme or accepting that submodule auth will silently fall back to anonymous. We chose to exclude it and noted `DROVER_AUTO_GIT_FLAGS` as a future escape hatch if operators need it badly enough to pass raw flags themselves.

**Radicle deferred**
Radicle uses a different CLI (`rad clone`), a different identity model (node-managed, not per-clone credentials), and likely requires a running node process in the container image. Sesigning this well requires understanding what a Radicle-ready base image looks like. It is tracked in a separate draft RFC rather than stretching this one.

## Consequences

- The `error_code` column gets a new enum value: `git_clone_failed`
- The `containers` table gets two new columns: `git_ref TEXT`, `git_sha TEXT`
- The `ready` wire message gains optional git fields
- `Agent.on_connect()` gains a side effect (reads env, may run git); subclasses calling `super().on_connect()` inherit this behavior automatically
- Operators using this feature need to increase `DROVER_INIT_TIMEOUT_SECONDS` beyond the 20-second default

## Related

- @docs/rfc/2026-04-22-better-startup-flow.md — drover.yaml setup commands that run after the clone
- @docs/rfc/TODO-radicle-git-clone.md — Radicle clone support (deferred)
- @docs/decisions/2026-04-21-ready-message-for-container-init.md — the ready signal design this feature builds on
