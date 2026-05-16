# Drover CLI

> Draft for team review — not yet adopted.
>
> **Status: blocked** — waiting on `docs/planning/websocket-streaming-plan.md` to land before the exec command can be implemented with real streaming output.

---

## Goal / Desired Outcome

A lightweight CLI tool (`drover`) that lets developers interact with the Drover orchestrator from a terminal without writing HTTP requests by hand. The goal is everyday usability: list images, launch a micro-container, run a command in it, and see output — all in one or two shell commands.

---

## Background

The orchestrator exposes a REST API over HTTP. Today the only first-class client is the webapp. Developers doing local work or scripting automation have to use `curl` directly, which requires knowing container IDs, polling for exec results, and manually threading API keys through each request.

Relevant context:
- `docs/exec-commands.md` — how commands flow from caller → orchestrator → guest agent
- `docs/planning/websocket-streaming-plan.md` — WebSocket streaming is in-flight; exec output streaming in the CLI will depend on this landing

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

Thin wrappers over `GET /images` and `GET /images/{name}`. Output as a table for list, key-value pairs for detail.

#### `drover ps`

Lists containers from `GET /containers`. Shows ID, image, status, label, age. Useful to grab a container ID for subsequent commands.

#### `drover start <image-name>`

Maps to `POST /containers`. Flags:

| Flag | API field | Notes |
|---|---|---|
| `--privileged` | `privileged: true` | Boolean flag |
| `--label <label>` | `label` | Arbitrary string |
| `--env KEY=VALUE` | `env` dict | Repeatable |
| `--timeout <seconds>` | `timeout_seconds` | Default: server default (300s) |

On success, prints the container ID so it can be captured: `$(drover start myimage)`.

#### `drover stop` / `drover destroy`

POST to the appropriate stop/destroy endpoint. Print status on completion.

#### `drover exec <container-id> [command...]`

This is the interesting one — see Open Questions below.

**Non-interactive** (`drover exec <id> git clone ...`): Posts to `POST /containers/{id}/execs` with the joined command string, then streams output to the terminal as it arrives. Exits with the command's exit code.

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

**Exec streaming depends on WebSocket plan** — The polling exec API works today but would feel broken in a CLI (you'd have to wait for the full command to finish before seeing any output). The CLI should use WebSockets for streaming once that lands. In the interim, the CLI could poll and print incrementally, but that's a stopgap worth calling out explicitly.

---

## Open Questions

**1. Interactive exec: should we support it in v1?**

`drover exec <id>` with no command argument dropping into an interactive shell is the most compelling developer experience, but it needs:
- PTY allocation at the orchestrator level (not currently planned)
- Bidirectional stdin streaming (also not planned)
- Raw terminal mode handling in the CLI

The WebSocket streaming plan mentions interactive shell as a future consideration. The question is whether we want to scope it into the CLI v1 or ship non-interactive exec first and revisit.

Options:
- **a)** Non-interactive only in v1. No-arg `drover exec` errors with a clear "not yet supported" message.
- **b)** Interactive in v1, which means PTY support needs to be designed and implemented first — probably a meaningful addition to the orchestrator scope.
- **c)** Non-interactive now, interactive as a fast follow once the WebSocket plan ships.

Option (c) seems most pragmatic but the team should confirm.

**2. How does the CLI handle streaming before WebSockets land?**

If we want to ship the CLI before the WebSocket streaming plan is complete, the exec command has to either (a) poll and print incrementally, or (b) wait until the command finishes and print everything at once. Neither is great UX. Should the CLI wait for WebSockets, or ship with polling and upgrade later?

**3. Output format**

Tables are readable for humans but bad for scripting. Should there be a `--json` flag for machine-readable output? A `--quiet` flag that prints only the ID? Worth deciding before implementation so it's consistent across commands.

**4. Container ID prefix matching**

Typing full container IDs is painful. Should the CLI accept unambiguous prefixes (like Docker does)? Straightforward to implement but slightly more complexity in the client.

---

## Implementation Notes

The CLI would live in a new top-level directory (e.g. `cli/`) and be a separate installable package. It talks to the orchestrator REST API (and eventually WebSocket) as an HTTP client — no direct database or socket access.

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

For exec streaming, the client will eventually open a WebSocket (per the streaming plan), subscribe to the command's output, and forward chunks to stdout/stderr as they arrive.

---

## Risks and Mitigations

**Exec streaming dependency** — The most useful part of `drover exec` blocks on the WebSocket plan. Mitigation: ship images/ps/start/stop first; treat exec as a phase 2 or explicitly wait on the streaming plan.

**Interactive mode scope creep** — PTY support is a significant orchestrator change. If we don't decide on interactive mode before starting the CLI build, it could end up being re-architected later. Mitigation: make the open question above a concrete decision before implementation starts.

**Auth token in process list** — If `DROVER_API_KEY` ever gets passed as a CLI flag instead of an env var, it would appear in `ps` output. Env vars are safer. Keep it env-only.

---

## Documentation Impact

When this ships:
- Add `docs/` page for CLI installation and usage
- Update `README.md` to mention the CLI as a first-class way to interact with the orchestrator
- `docs/exec-commands.md` may need a note about how the CLI's streaming maps to the exec flow
