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
drover images                              List available images
drover image <name>                        Show details for an image

drover ps                                  List micro-containers
drover start <image-name> [flags]          Launch a micro-container
drover stop <container-id>                 Stop a running container
drover destroy <container-id>             Destroy a container

drover exec <container-id> [command...]    Run a command (or drop into interactive)
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

POST to the appropriate stop/destroy endpoint. Returns a JSON object describing the resulting state (e.g. `{"id": "...", "status": "stopped"}`).

#### `drover exec <container-id> [command...]`

This is the interesting one — see Open Questions below.

**Non-interactive** (`drover exec <id> git clone ...`): Posts to `POST /containers/{id}/execs` with the joined command string, then streams output to the terminal as it arrives. `stdout` and `stderr` chunks are forwarded to the corresponding CLI streams as raw bytes (not wrapped in JSON) so that command output remains pipeable as-is. The process exits with the command's exit code. (See Open Questions on whether a `--json` mode that wraps the stream in newline-delimited JSON frames is worth adding.)

**Interactive** (`drover exec <id>` with no command): Would drop the user into an interactive shell inside the container. This requires PTY support in the orchestrator, which doesn't exist yet.

---

## Alternatives Considered

**Shell script wrapper around curl** — Gets you 80% of the way with no new code, but exec output streaming and interactive mode are not feasible without a real client. Also hard to distribute.

**SDK library only, no CLI** — A Python or JS client library is probably the right abstraction layer eventually, but a CLI is more immediately useful for humans and is easy to build on top of the same library.

**Config file for auth (e.g. `~/.drover/config`)** — More ergonomic for switching profiles, but adds complexity (file format, multi-profile support, precedence rules). Env vars are simpler and already standard for this kind of tool. Can revisit if multi-environment use becomes common.

---

## Key Decisions

**Python + Click** is the natural fit: the orchestrator is already Python, the team knows it, and Click produces good UX without much boilerplate. If the CLI ever needs to be distributed as a standalone binary, PyInstaller or similar can wrap it.

**`drover start` not `drover run`** — "run" implies synchronous execution. Starting a container is an async operation that hands back a container ID; the caller decides what to do next. "Start" is more accurate.

**Exec streaming uses the WebSocket endpoint** — The polling exec API works today but would feel broken in a CLI (you'd have to wait for the full command to finish before seeing any output). The CLI uses the per-container WebSocket endpoint (`/containers/{id}/ws`) to receive output frames as they arrive and forwards them to stdout/stderr.

**All control-plane output is JSON** — Every command that returns data (everything except `exec`'s streamed output) prints a single JSON value to stdout. Even commands that conceptually return a single scalar — `drover start` returning a container ID, `drover stop` returning a status — emit a JSON object (`{"id": "..."}`, `{"id": "...", "status": "stopped"}`) rather than a bare string. This is deliberate:

- The shape of the response can grow over time (extra fields, nested metadata) without breaking callers that select specific fields with `jq`.
- One consistent contract across every command is easier to learn and document than a mix of tables, key-value text, and bare IDs.
- `jq` is universally available and makes scripting against the CLI straightforward: `drover ps | jq -r '.[] | select(.status=="running") | .id'`.

Errors are also emitted as JSON on stderr (`{"error": "...", "detail": "..."}`) with a non-zero exit code. Human-friendly table rendering, if it's ever wanted, can be added later as an opt-in `--format=table` flag without breaking the default contract.

---

## Open Questions

**1. Interactive exec: should we support it in v1?**

`drover exec <id>` with no command argument dropping into an interactive shell is the most compelling developer experience, but it needs:
- PTY allocation at the orchestrator level (not currently planned)
- Bidirectional stdin streaming (also not planned)
- Raw terminal mode handling in the CLI

Interactive shell support over the WebSocket transport is listed as a future consideration in the [WebSocket ADR](../decisions/2026-04-11-websockets-for-streaming.md); the current endpoint is one-way (server → client only). The question is whether we want to scope interactive into the CLI v1 or ship non-interactive exec first and revisit.

Options:
- **a)** Non-interactive only in v1. No-arg `drover exec` errors with a clear "not yet supported" message.
- **b)** Interactive in v1, which means PTY support and bidirectional stdin need to be designed and implemented on the orchestrator side first — probably a meaningful addition to the orchestrator scope.
- **c)** Non-interactive now, interactive as a fast follow once orchestrator-side stdin/PTY work lands.

Option (c) seems most pragmatic but the team should confirm.

**2. Exec output framing**

Control-plane output is JSON (see Key Decisions), but `drover exec` streams raw bytes so its output is directly pipeable. Should there also be an opt-in `--json` mode for `exec` that wraps each chunk in a newline-delimited JSON frame (e.g. `{"stream": "stdout", "data": "..."}` ... `{"exit_code": 0}`)? Useful for callers that want structured access to exit codes and stream separation without parsing the raw byte stream. Defer until there's a concrete need.

**3. Container ID prefix matching**

Typing full container IDs is painful. Should the CLI accept unambiguous prefixes (like Docker does)? Straightforward to implement but slightly more complexity in the client.

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

For exec streaming, the client opens a WebSocket to `/containers/{id}/ws`, filters incoming messages by the `command_id` returned from the `POST /containers/{id}/execs` call, and forwards `output` chunks to stdout/stderr as they arrive. It exits when the matching `status: complete` message arrives, propagating `exit_code`.

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
