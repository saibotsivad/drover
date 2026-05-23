# Drover CLI

> Draft for team review — not yet adopted.

---

## Goal / Desired Outcome

A lightweight CLI tool (`drover`) that lets developers interact with the Drover orchestrator from a terminal without writing HTTP requests by hand. The goal is everyday usability: list images, launch a micro-container, run a command in it, and see output — all in one or two shell commands.

---

## Background

The orchestrator exposes a REST API over HTTP. Today the only first-class client is the webapp. Developers doing local work or scripting automation have to use `curl` directly, which requires knowing container IDs, polling for exec results, and manually threading API keys through each request.

Relevant context:
- `docs/exec-commands.md` — how commands flow from caller → orchestrator → guest agent
- `orchestrator/README.md` (WebSocket stream section) — the per-container WebSocket endpoint the CLI will use for exec output streaming

---

## Proposal

### Authentication

No config file. Two environment variables, set once in the shell profile:

```sh
export DROVER_API_URL=https://drover.example.com
export DROVER_API_KEY=sk-...
```

The CLI errors clearly on startup if either is missing. This keeps the tool stateless and makes it easy to swap between environments.

### Command Surface

```
drover images                                  List available images
drover image <name>                            Show details for an image

drover ps                                      List micro-containers
drover start <image-name> [flags]              Launch a micro-container
drover (stop|destroy) <container-id> [flags]   Stop or destroy a container
                                               (blocks until terminal state by default)

drover exec <container-id> -- <command...>     Run a command in a container
```

#### `drover images` / `drover image <name>`

Thin wrappers over `GET /images` and `GET /images/{name}`. Returns a JSON array of image objects for the list form, and a single JSON image object for the detail form.

#### `drover ps`

Lists containers from `GET /containers`. Returns a JSON array of container objects (id, image, status, label, age, etc.). Useful to grab a container ID for subsequent commands via `jq`.

#### `drover start <image-name>`

Maps to `POST /containers`. Flags:

| Flag | API field | Notes |
|---|---|---|
| `--privileged` | `privileged: true` | Boolean flag |
| `--label <label>` | `label` | Arbitrary string |
| `--env KEY=VALUE` | `env` dict | Repeatable |
| `--timeout <seconds>` | `timeout_seconds` | Default: server default (300s) |

On success, prints a JSON object `{"id": "<container-id>"}` so the ID can be captured: `id=$(drover start myimage | jq -r .id)`.

#### `drover (stop|destroy) <container-id>`

POST to the appropriate stop/destroy endpoint. The orchestrator returns immediately with the container in `stopping` / `destroying`; by default the CLI then polls `GET /containers/{id}` until the container reaches the terminal state (`stopped` / `destroyed`) and prints a JSON object describing the resulting state, e.g. `{"id": "...", "status": "stopped"}`.

Flags:

| Flag | Default | Notes |
|---|---|---|
| `--no-wait` | off | Return as soon as the transition is accepted. The printed JSON reflects the transitional state (`"stopping"` / `"destroying"`). |
| `--timeout <seconds>` | 30 (`stop`), 60 (`destroy`) | Maximum time to wait for the terminal state. On timeout, exit non-zero and write a JSON error to stderr, e.g. `{"error": "timeout", "id": "...", "status": "stopping"}`. |
| `--interval <seconds>` | 1 | Seconds between poll requests while waiting. Ignored with `--no-wait`. |

#### `drover exec <container-id> -- <command...>`

The command to run inside the container is separated from `drover`'s own arguments by `--`. Everything after `--` is forwarded verbatim, so caller-side flags and quoting can't be misinterpreted as CLI flags (matches the convention used by `kubectl exec`, `docker exec`, `cargo run`, etc.).

The CLI posts to `POST /containers/{id}/execs` with the joined command string, opens the per-container WebSocket at `/containers/{id}/ws`, filters incoming frames by the returned `command_id`, and writes each matching frame to stdout as newline-delimited JSON exactly as it arrives from the orchestrator — i.e. one frame per line, of the form:

```jsonc
{"type": "output", "command_id": "...", "stream": "stdout", "data": "..."}
{"type": "output", "command_id": "...", "stream": "stderr", "data": "..."}
{"type": "status", "command_id": "...", "status": "complete", "exit_code": 0}
```

This keeps the CLI a thin pass-through over the WebSocket: no re-shaping, no demultiplexing into the CLI's own stdout/stderr, no base64 unwrapping. Consumers reconstruct the command's stdout/stderr with `jq`:

```sh
drover exec $id -- ls -la \
  | jq -r 'select(.type=="output" and .stream=="stdout") | .data'
```

The CLI exits when the matching `status: complete` frame arrives, propagating its `exit_code` as the process exit code. Container-wide `log` frames and frames for other `command_id`s are dropped (use a future `drover logs` command for those).

**Interactive mode is explicitly out of scope for v1.** Bare `drover exec <id>` (no `--` and no command) errors with a clear "interactive exec not yet supported" message. When interactive lands later, that same `--`-less form will be the trigger, so reserving the syntax now keeps the future addition non-breaking.

---

## Alternatives Considered

**Shell script wrapper around curl** — Gets you 80% of the way with no new code, but exec output streaming and interactive mode are not feasible without a real client. Also hard to distribute.

**SDK library only, no CLI** — A Python or JS client library is probably the right abstraction layer eventually, but a CLI is more immediately useful for humans and is easy to build on top of the same library.

**Config file for auth (e.g. `~/.drover/config`)** — More ergonomic for switching profiles, but adds complexity (file format, multi-profile support, precedence rules). Env vars are simpler and already standard for this kind of tool. Can revisit if multi-environment use becomes common.

---

## Key Decisions

**Python + Click** is the natural fit: the orchestrator is already Python, the team knows it, and Click produces good UX without much boilerplate. If the CLI ever needs to be distributed as a standalone binary, PyInstaller or similar can wrap it.

**`drover start` not `drover run`** — "run" implies synchronous execution. Starting a container is an async operation that hands back a container ID; the caller decides what to do next. "Start" is more accurate.

**Exec streaming uses the WebSocket endpoint, and frames are passed through as-is** — The polling exec API works today but would feel broken in a CLI (you'd have to wait for the full command to finish before seeing any output). The CLI uses the per-container WebSocket endpoint (`/containers/{id}/ws`) to receive output frames as they arrive. Each frame is already a JSON object (e.g. `{"type": "output", "stream": "stdout", "data": "..."}`), and the CLI writes them to stdout as newline-delimited JSON without re-shaping. This keeps `exec`'s output contract consistent with the "everything is JSON" decision below, and avoids the CLI having to guess how callers want stdout/stderr framing handled.

**`drover (stop|destroy)` blocks by default** — These endpoints transition the container to `stopping` / `destroying` and return immediately. The CLI polls `GET /containers/{id}` until the terminal state is reached so that `drover stop X && drover destroy X` and similar compositions do the obvious thing without callers having to write their own `until` loops. `--no-wait` is the escape hatch for fire-and-forget scripting, and `--timeout` / `--interval` give callers control over the polling loop's bounds. The shape of the returned JSON is the same in both modes — only the `status` field differs (terminal vs. transitional).

**All control-plane output is JSON** — Every command that returns data (everything except `exec`'s streamed output) prints a single JSON value to stdout. Even commands that conceptually return a single scalar — `drover start` returning a container ID, `drover stop` returning a status — emit a JSON object (`{"id": "..."}`, `{"id": "...", "status": "stopped"}`) rather than a bare string. This is deliberate:

- The shape of the response can grow over time (extra fields, nested metadata) without breaking callers that select specific fields with `jq`.
- One consistent contract across every command is easier to learn and document than a mix of tables, key-value text, and bare IDs.
- `jq` is universally available and makes scripting against the CLI straightforward: `drover ps | jq -r '.[] | select(.status=="running") | .id'`.

Errors are also emitted as JSON on stderr (`{"error": "...", "detail": "..."}`) with a non-zero exit code. Human-friendly table rendering, if it's ever wanted, can be added later as an opt-in `--format=table` flag without breaking the default contract.

---

## Open Questions

**1. Container ID prefix matching**

Typing full container IDs is painful. Should the CLI accept unambiguous prefixes (like Docker does)? Straightforward to implement but slightly more complexity in the client.

---

## Resolved Questions

**Interactive exec is out of scope for v1.** Non-interactive only. The `--`-less form (`drover exec <id>`) is reserved for interactive in a future version — until then it errors with a clear "not yet supported" message. Interactive lands as a fast follow once orchestrator-side PTY and bidirectional stdin support exist (currently the WebSocket is one-way, per the [WebSocket ADR](../decisions/2026-04-11-websockets-for-streaming.md)).

**Exec output framing: pass through WebSocket frames as NDJSON.** The orchestrator already emits structured JSON frames over the WebSocket; the CLI writes them to stdout one per line without transformation. Callers reconstruct stdout/stderr (or pluck out `exit_code`) with `jq`. This avoids a second framing decision in the CLI and keeps `exec` consistent with the rest of the JSON-output contract.

---

## Implementation Notes

The CLI would live in a new top-level directory (e.g. `cli/`) and be a separate installable package. It talks to the orchestrator REST API for control-plane operations and the per-container WebSocket endpoint for exec output streaming — no direct database or socket access.

Rough structure:
```
cli/
  drover/
    __init__.py
    main.py          # Click group
    client.py        # HTTP client wrapper (httpx)
    commands/
      images.py
      containers.py
      exec.py
  pyproject.toml
```

The HTTP client layer wraps httpx, reads `DROVER_API_URL`/`DROVER_API_KEY` from the environment, and handles error responses uniformly (print the `detail` field and exit non-zero).

For exec streaming, the client opens a WebSocket to `/containers/{id}/ws`, filters incoming messages by the `command_id` returned from the `POST /containers/{id}/execs` call, and writes each matching frame to stdout as a newline-delimited JSON object (no re-shaping — the orchestrator's frame is the contract). It exits when the matching `status: complete` frame arrives, propagating `exit_code` as the process exit code. Non-matching frames (other `command_id`s, container `log` frames) are dropped.

For `drover (stop|destroy)`, after the initial POST the client polls `GET /containers/{id}` every `--interval` seconds until the container reaches the terminal state or `--timeout` is hit. The timeout is wall-clock; the interval is sleep-between-requests, so request latency doesn't shorten it. On timeout the client writes `{"error": "timeout", "id": "...", "status": "<transitional>"}` to stderr and exits non-zero.

---

## Risks and Mitigations

**Interactive mode scope creep** — PTY and bidirectional stdin support are a significant orchestrator change (the current WebSocket is one-way). If we don't decide on interactive mode before starting the CLI build, it could end up being re-architected later. Mitigation: make the open question above a concrete decision before implementation starts.

**Auth token in process list** — If `DROVER_API_KEY` ever gets passed as a CLI flag instead of an env var, it would appear in `ps` output. Env vars are safer. Keep it env-only.

---

## Documentation Impact

When this ships:
- Add `docs/` page for CLI installation and usage
- Update `README.md` to mention the CLI as a first-class way to interact with the orchestrator
- `docs/exec-commands.md` may need a note about how the CLI's streaming maps to the exec flow
