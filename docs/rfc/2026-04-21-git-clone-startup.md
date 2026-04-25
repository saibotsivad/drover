# Goal

Support git clone at startup, before the `ready` signal — shipped as an **optional startup plugin** that plugs into a new generic initializer mechanism on the executor.

# Summary

Introduce a plugin model on the `Agent` class: zero or more initializers run during `on_connect()`, each owns a unique id, and each emits a standardized `initializing` message to the executor, which sends it over the socket before the final `ready` message is sent.

Ship an example `auto-git` plugin, in a separate package, that clones a repo when `DROVER_AUTO_GIT_URL` is set.

The core executor stays plugin-agnostic: consumers who don't want a plugin don't pay for it. Other plugin-specific startup concerns are additive packages rather than changes to the core.

# Non-Goals

- **Non-git code checkout** — This RFC is strictly for git over http or ssh
- **git submodules** — Adds complexity and can happen later
- **Per-container init timeout override** — The global `DROVER_INIT_TIMEOUT_SECONDS` is the only knob for now
- **Progress tracking/streaming within a plugin** — each plugin returns a single payload for an `initializing` message, and no loading progress is tracked or inferred
- **Plugin auto-discovery via Python entry points** — plugin are explicit composition only

---

# Plugin Model

## The Initializer protocol

An initializer is any object with a unique `id` and an async `run()`:

```python
class Initializer(Protocol):
    id: str  # unique per agent; validated against the id grammar below

    async def run(self, context: InitContext) -> dict | None:
        """Perform startup work. Return a JSON-serialisable dict that will be
        included in the initializing message's data field, or None to send
        the message with no data."""
```

`InitContext` exposes the process environment and a logger; additional fields can be added without breaking the protocol.

The `Agent` constructor accepts an ordered list of initializers:

```python
agent = Agent(initializers=[GitCloneInitializer(), DroverYamlInitializer()])
```

## Plugin id grammar

Plugin ids must match `^[a-z][a-z0-9-]*$`, 1–64 characters. The `drover-` prefix is reserved for first-party plugins. Ids must be unique within an agent's initializer list — if you need to run the same plugin twice (e.g. two git clones), provide two instances with distinct ids.

First-party ids:
- `auto-git` — the git clone plugin described in this RFC

## Execution flow

On `on_connect()`, the base `Agent` iterates the initializer list in order:

1. Call `await initializer.run(context)`
2. On success, send `{type: "initializing", plugin: <id>, data: <result or null>}`
3. On failure, send `{type: "init_failed", plugin: <id>, error: {code, message}}`, then stop — `ready` is not sent

If the list is empty (the default), `on_connect()` is a no-op and `ready` is sent immediately, preserving current behaviour.

Subclasses that override `on_connect()` and want the initializer chain should call `await super().on_connect()`.

---

# Environment Variables (auto-git plugin)

| Variable | Required | Default | Description |
|---|---|---|---|
| `DROVER_AUTO_GIT_URL` | To enable plugin | _(unset)_ | URL of the repository to clone |
| `DROVER_AUTO_GIT_REF` | no | Repo default branch | Branch name, tag, or full commit SHA to check out |
| `DROVER_AUTO_GIT_DEPTH` | no | `1` | Shallow clone depth; set to `0` to clone full history |
| `DROVER_AUTO_GIT_TOKEN` | no | _(unset)_ | Bearer token for HTTPS authentication (GitHub, GitLab, Gitea, etc.) |
| `DROVER_AUTO_GIT_SSH_KEY` | no | _(unset)_ | Base64-encoded PEM private key for SSH authentication |

These are passed to the micro-container via the `env` dict in `POST /containers`, the same as any other container environment variable. The orchestrator does not inspect or act on them; they're forwarded to Docker and read by the plugin at startup.

The `auto-git` plugin no-ops (returns `None` without sending a message) when `DROVER_AUTO_GIT_URL` is unset, so operators can include the plugin in a base image and toggle it per-container via env vars.

---

# Clone Destination

The repo is always cloned to `/workspace`, this is fixed and not configurable. A consistent path lets subsequent commands be written portably without knowing the repo name. The directory name `/workspace` is short, obvious, and avoids collisions with system paths. After the clone, the plugin sets this as the default working directory for all subsequent commands run by the agent.

---

# Authentication

## HTTPS with token

Set `DROVER_AUTO_GIT_TOKEN`. The plugin rewrites the URL to embed credentials using git's credential-helper protocol:

```sh
git -c credential.helper='!f() { echo "username=x-token"; echo "password=$TOKEN"; }; f' clone <url> /workspace
```

This works for GitHub (personal access tokens and fine-grained tokens), GitLab, Gitea, Forgejo, and Bitbucket.

The token is never written to disk, the `-c` flag scopes the credential helper to this invocation only.

The URL in `DROVER_AUTO_GIT_URL` should be a plain HTTPS URL (e.g. `https://github.com/org/repo`), not an already-embedded `https://token@host/...` form as that form will leak the token into log lines.

## SSH

Set `DROVER_AUTO_GIT_SSH_KEY` to a base64-encoded PEM private key (the kind you'd normally store in `~/.ssh/id_ed25519`). The plugin writes the decoded key to a temporary file under `/tmp`, sets `GIT_SSH_COMMAND` to use it with strict host checking disabled, clones, then shreds the file:

```sh
install -m 600 /dev/null /tmp/drover_id
base64 -d <<< "$KEY" > /tmp/drover_id
GIT_SSH_COMMAND='ssh -i /tmp/drover_id -o StrictHostKeyChecking=no' git clone <url> /workspace
shred -u /tmp/drover_id
```

Setting `StrictHostKeyChecking=no` is intentional for ephemeral containers — there's no host-key database to trust against, and adding a known-hosts bootstrap step would be more friction than the threat model warrants.

## Unauthenticated (public repos)

If neither `DROVER_AUTO_GIT_TOKEN` nor `DROVER_AUTO_GIT_SSH_KEY` is set, the plugin runs a plain `git clone`. This works for any public repository over HTTPS.

## Priority

If both token and SSH key are provided, SSH key wins and a warning is logged.

---

# Ref Checkout

After cloning, if `DROVER_AUTO_GIT_REF` is set, the plugin runs:

```sh
git -C /workspace checkout <ref>
```

This handles branches, tags, and commit SHAs uniformly. If the ref does not exist, the checkout fails and the plugin raises (see Failure Handling).

If `DROVER_AUTO_GIT_REF` is not set, the clone lands on whatever the remote's HEAD points to (almost always the default branch).

---

# Failure Handling

Initializer failures are reported explicitly via a new `init_failed` message rather than by silently letting the init timeout fire:

```json
{
  "type": "init_failed",
  "plugin": "auto-git",
  "error": {
    "code": "clone_failed",
    "message": "fatal: repository not found"
  }
}
```

On receipt, the orchestrator transitions the container to `error` with `error_code: init_plugin_failed` and records the failing plugin id in a new column `failed_plugin TEXT`. The container is torn down the same way as other init failures.

The `init_timeout` watchdog remains as a fallback for cases where the agent can't get a message out at all (network partition, process crash, image missing the agent binary).

Each plugin picks its own `error.code` value. For `auto-git`, codes include `clone_failed`, `checkout_failed`, `auth_missing`; the full list will be documented alongside the plugin.

The plugin logs the exact git command (with token redacted) and its stderr output before raising, so operators can diagnose the failure from container logs.

---

# Orchestrator Considerations

## Init timeout

The global `DROVER_INIT_TIMEOUT_SECONDS` (default 20 seconds) must be long enough to cover all initializer work. Twenty seconds is tight for anything but tiny repos on fast links. Operators using `auto-git` should raise this to at least 60–120 seconds.

## Init metadata storage

Instead of adding per-plugin columns to `containers`, add a single JSON column:

```
init_metadata JSONB  -- map of plugin_id -> data dict
failed_plugin TEXT   -- set when error_code = init_plugin_failed
```

The orchestrator populates `init_metadata` by collecting the `data` fields from each `initializing` message it receives on the socket during init. `GET /containers/{id}` surfaces the column verbatim.

For `auto-git` specifically, the stored shape is:

```json
{
  "auto-git": {
    "workdir": "/workspace",
    "git_ref": "main",
    "git_sha": "a89a33e4f8b1c...",
    "duration_ms": 4210
  }
}
```

## Late messages

`initializing` messages arriving after `ready` are ignored, mirroring the existing policy for late `ready` messages.

## Secrets

Environment variables passed in `POST /containers` are forwarded verbatim to Docker and never written to the `containers` table. `DROVER_AUTO_GIT_TOKEN` and `DROVER_AUTO_GIT_SSH_KEY` never touch the database — they live only in Docker's memory for the container's lifetime.

Operators should avoid logging full create request bodies when they contain secrets, and should prefer short-lived tokens where possible.

---

# Wire Protocol Changes

Two new guest → orchestrator message types:

**`initializing`** — emitted once per plugin on successful completion:

```json
{
  "type": "initializing",
  "plugin": "auto-git",
  "data": {
    "workdir": "/workspace",
    "git_ref": "main",
    "git_sha": "a89a33e4f8b1c...",
    "duration_ms": 4210
  }
}
```

The `data` field is a JSON-serialisable object chosen by the plugin, or `null`. The core protocol does not interpret it.

**`init_failed`** — emitted on plugin failure; no further initializers run and `ready` is not sent:

```json
{
  "type": "init_failed",
  "plugin": "auto-git",
  "error": {
    "code": "clone_failed",
    "message": "fatal: repository not found"
  }
}
```

The `ready` message stays minimal:

```json
{
  "type": "ready",
  "duration_ms": 4210
}
```

`duration_ms` covers the full `on_connect` wall time (all initializers combined).

---

# Executor Implementation

## Core package (`drover-executor`)

The core package stays zero-dependency and git-agnostic. Changes:

- New `drover_executor/initializer.py` exporting the `Initializer` protocol and `InitContext` dataclass
- `Agent` constructor accepts `initializers: list[Initializer] | None = None`
- Base `Agent.on_connect()` iterates `self.initializers`, emitting `initializing` / `init_failed` messages per the flow above
- `protocol.py` gains `encode_initializing(plugin, data)`, `encode_init_failed(plugin, code, message)`, and `duration_ms` on `encode_ready`
- Id validation lives in the core and raises at `Agent.__init__` time if any id is malformed or duplicated

## Git plugin package (`drover-executor-git`)

The git clone feature ships as its own package so it can be installed only where needed:

```
drover-executor-git/
  pyproject.toml                     # depends on drover-executor
  drover_executor_git/
    __init__.py                      # exports GitCloneInitializer
    plugin.py                        # implementation
```

`GitCloneInitializer` has `id = "auto-git"` and its `run()` implements the clone + checkout + rev-parse sequence described above.

Users opt in by composing it into their agent:

```python
from drover_executor import Agent
from drover_executor_git import GitCloneInitializer

agent = Agent(initializers=[GitCloneInitializer()])
asyncio.run(agent.run())
```

For operators who want the default CLI experience with git, we'll ship a convenience entry point `drover-executor-with-git` in the git package that wires the plugin in automatically. The core `drover-executor` CLI remains plugin-free.

Subclasses that need custom behaviour on top of the initializer chain call `await super().on_connect()` first, as before.

---

# ADR

**Date:** 2026-04-21
**Status:** proposed
**Last revised:** 2026-04-24

## Context

Drover micro-containers are ephemeral. Today there is no built-in way to get a code checkout into a container before it signals `ready`. Operators work around this by baking repos into images (slow iteration) or running clone commands after `ready` (means the container is "ready" before it's actually useful). We want git clone to be a first-class startup step.

At the same time, git is not the only startup concern on the horizon — drover.yaml project setup, Radicle clone, tarball fetch, S3 pull, and others are likely to follow. Baking git directly into the core `Agent` locks us into a pattern where every future startup feature grows the core and its wire protocol. A plugin model lets the core stay small while startup features evolve as additive packages.

## Decisions

**Plugin-based initialization over hardcoded `on_connect`**

The previous revision of this RFC put `run_git_clone()` directly inside `Agent.on_connect()`. That coupled the core library to a specific feature and would have forced every future startup concern to follow the same pattern. A plugin interface keeps the core git-agnostic, makes features composable, and keeps test surfaces small and independent.

**Standardized `initializing` / `init_failed` messages over plugin-specific ready fields**

The previous revision added `workdir`, `git_ref`, `git_sha`, and `duration_ms` to the `ready` message. Those fields are git-specific and their presence in a core protocol is a layering violation. A generic `initializing` message with a plugin-owned `data` payload keeps the protocol stable as plugins come and go, and gives operators visibility into each init step as it happens rather than one opaque blob at the end.

**Explicit `init_failed` message over relying on init timeout**

A plugin failure could be detected indirectly by waiting for `DROVER_INIT_TIMEOUT_SECONDS` to fire, but that's slow and opaque. An explicit failure message lets the orchestrator transition the container immediately and include the failing plugin id and a plugin-chosen error code in the response. The watchdog stays as a fallback for cases where the agent can't send a message at all.

**Generic `init_plugin_failed` error code + `failed_plugin` column, not a per-plugin enum**

A naive design would add `git_clone_failed`, `drover_yaml_failed`, and so on to the `error_code` enum as plugins ship. Instead, a single `init_plugin_failed` value plus a separate `failed_plugin` column keeps the enum flat and makes adding plugins a schemaless operation from the orchestrator's perspective.

**Single `init_metadata` JSON column, not a separate events table**

A normalised `container_init_events` table would be more queryable and scale to in-flight progress messages, but for v1 the simpler JSON column covers the retrospective "what did each plugin produce" need. A future RFC can move to a table when progress streaming lands.

**Git plugin ships in a separate package (`drover-executor-git`)**

Keeping the plugin in its own package guarantees that operators who don't want git don't pay for it (no import, no code path). It also sets the precedent for future plugins — each is a package, each is opt-in, each can evolve independently.

**Plugin id grammar: `^[a-z][a-z0-9-]*$`, `drover-` prefix reserved**

A tight grammar keeps ids unambiguous in log lines and DB keys. Reserving `drover-` for first-party plugins keeps the namespace clean without requiring a registry. Core validates ids at agent construction time to catch typos early.

**Fixed clone destination (`/workspace`)**

We considered making the path configurable via `DROVER_AUTO_GIT_DIR` but we rejected it for v1 because a fixed path makes shell commands written against Drover containers portable across repos — you don't need to know the repo name or parameterize it. If a real use case emerges (e.g. monorepo subdirectory workflows), a future RFC can add the variable.

**No submodule support in v1**

Although `--recurse-submodules` is one flag, each submodule URL may need independent credentials, and our current auth model is per-clone, not per-URL. Supporting submodules properly would require either a more complex credential scheme or accepting that submodule auth will silently fall back to anonymous. We chose to exclude it and noted `DROVER_AUTO_GIT_FLAGS` as a future escape hatch if operators need it badly enough to pass raw flags themselves.

**Radicle deferred**

Radicle uses a different CLI (`rad clone`), a different identity model (node-managed, not per-clone credentials), and likely requires a running node process in the container image. Designing this well requires understanding what a Radicle-ready base image looks like. Under the plugin model it lands as a sibling package (`drover-executor-radicle`) rather than stretching this one. Tracked in a separate draft RFC.

## Consequences

- Core `drover-executor` gains an initializer protocol, two new protocol message encoders, and a constructor argument; otherwise unchanged
- Git clone ships as a new `drover-executor-git` package
- The `error_code` column gets one new enum value: `init_plugin_failed`
- The `containers` table gets two new columns: `init_metadata JSONB`, `failed_plugin TEXT`
- The wire protocol gains two new message types: `initializing`, `init_failed`
- The `ready` message gains `duration_ms`; no git-specific fields are added to the core protocol
- Operators using `auto-git` need to increase `DROVER_INIT_TIMEOUT_SECONDS` beyond the 20-second default
- Future startup features (drover.yaml, Radicle, etc.) land as sibling plugin packages — no core changes required

## Related

- @docs/rfc/2026-04-22-better-startup-flow.md — drover.yaml setup commands; expected to ship as a `drover-yaml` plugin under this model
- @docs/rfc/TODO-radicle-git-clone.md — Radicle clone support, deferred; a sibling plugin
- @docs/decisions/2026-04-21-ready-message-for-container-init.md — the ready signal design this feature builds on
