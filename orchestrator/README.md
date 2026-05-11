# Orchestrator Container

The orchestrator is the core of Drover. It runs as a Docker container on the host, exposes a REST API for managing ephemeral micro-containers, and communicates with guest agents inside those containers over Unix sockets.

For a full system overview, see the [top-level README](../README.md).

## Contents

- [How it fits together](#how-it-fits-together)
- [Configuration](#configuration)
- [Mounts](#mounts)
- [API reference](#api-reference)
- [Container lifecycle](#container-lifecycle)
- [Authentication](#authentication)
- [Socket protocol](#socket-protocol)
- [Database](#database)
- [Logging](#logging)
- [Testing](#testing)
- [Versioning and releases](#versioning-and-releases)

## How it fits together

```
Host (bare metal, rootless Docker)
└── orchestrator container
    ├── REST API  ←  callers (webapp, scripts, CI)
    ├── Docker client  →  creates/stops micro-containers
    └── Unix sockets  ↔  guest agents inside micro-containers
```

The orchestrator talks to the Docker daemon through the host socket (mounted in). It creates one Unix socket per micro-container, placed in a shared directory also mounted into each micro-container. The guest agent inside each container connects to that socket to send a `ready` signal and receive `exec` commands.

The optional [webapp](../webapp/README.md) is a management UI that sits in front of the orchestrator API. The [executor](../executor/README.md) library is what micro-container images use to implement the guest agent.

## Configuration

All configuration is via environment variables.

| Variable | Default | Description |
|---|---|---|
| `DROVER_API_KEY` | _(unset)_ | SHA-256 hash of the bearer token. When unset, authentication is disabled. |
| `PRIVILEGED_IMAGE` | _(unset)_ | Docker image name for privileged micro-containers. Required to use `"privileged": true` on container create. |
| `DB_PATH` | `/var/lib/orchestrator/db.sqlite` | Path to the SQLite database file. |
| `SOCKET_DIR` | `/var/run/microcontainers` | Directory where per-container Unix sockets are created. |
| `DOCKER_SOCK` | `/var/run/docker.sock` | Path to the Docker daemon socket. |
| `REAPER_INTERVAL_SECONDS` | `5` | How often (in seconds) the idle-timeout reaper runs. |
| `DROVER_INIT_TIMEOUT_SECONDS` | `20` | Seconds a container has to send `ready` before being marked `error`. |
| `LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

To enable authentication, generate a random token and store its SHA-256 hash:

```sh
TOKEN=$(openssl rand -hex 32)
echo "Token (give to callers): $TOKEN"
echo "Hash (set as DROVER_API_KEY): $(echo -n "$TOKEN" | sha256sum | awk '{print $1}')"
```

See [Authentication](#authentication) for details on how tokens are verified.

## Mounts

Three mounts are required for the orchestrator to function:

| Host path | Container path | Purpose |
|---|---|---|
| `/run/user/1000/docker.sock` | `/var/run/docker.sock` | Docker-out-of-Docker (rootless). Adjust the host path to match your UID. |
| `/var/run/microcontainers/` | `/var/run/microcontainers/` | Shared directory for per-container Unix sockets. Must also be mounted into each micro-container image. |
| `/var/lib/orchestrator/db.sqlite` | `/var/lib/orchestrator/db.sqlite` | Persistent SQLite database. The file must exist on the host before starting. |

The container entrypoint runs as root just long enough to detect the GID of the mounted `docker.sock`, add the `orchestrator` user to a group with that GID, and then drops privileges via `gosu`. This works for both rootful Docker (socket owned by `root:docker`) and rootless Docker (socket owned by the invoking user) without baking a GID into the image.

A minimal `docker-compose.yml` is provided in the [repo root](../docker-compose.yml).

## API reference

All endpoints except `GET /health` require authentication when `DROVER_API_KEY` is set (see [Authentication](#authentication)).

### Health

```
GET /health
```

Returns `{"healthy": true, "privileged_image": "<name or null>"}`. Not auth-gated; suitable for liveness probes.

### Containers

```
POST   /containers                        Create a micro-container
GET    /containers/{id}                   Get container state
POST   /containers/{id}/stop              Stop (resumable)
POST   /containers/{id}/resume            Resume a stopped container
DELETE /containers/{id}                   Stop and permanently destroy
POST   /containers/{id}/exec              Send a shell command
GET    /containers/{id}/exec/{cmd_id}     Poll command output
```

**Create request body:**

```json
{
  "image": "python-runner",
  "privileged": false,
  "env": { "MY_VAR": "value" },
  "label": "optional human label",
  "timeout_seconds": 300
}
```

| Field | Type | Default | Constraints |
|---|---|---|---|
| `image` | string | required | Alphanumeric, dots, hyphens, underscores, slashes; max 256 chars. Unless `privileged: true`, the value must match the `drover.name` label of an installed image (see [Images](#images)). |
| `privileged` | bool | `false` | Requires `PRIVILEGED_IMAGE` to be configured. |
| `env` | object | `{}` | Keys: POSIX identifiers, max 256 chars. Values: max 32 KB. |
| `label` | string | `null` | Printable chars; max 1024 chars. |
| `timeout_seconds` | int | `300` | Range 1–86400 (1 second to 24 hours). |

`POST /containers` returns immediately with the container in `initializing` state. Poll `GET /containers/{id}` until status is `running` before sending commands. See [Container lifecycle](#container-lifecycle) and the [container initialization doc](../docs/container-initialization.md) for details.

**Exec request/response:**

`POST /containers/{id}/exec` accepts `{"command": "git clone ..."}` and returns `{"command_id": "<id>"}` immediately. Poll `GET /containers/{id}/exec/{cmd_id}` for output:

```json
{
  "command_id": "abc123",
  "status": "complete",
  "exit_code": 0,
  "messages": [
    {"seq": 1, "stream": "stdout", "data": "Cloning into..."},
    {"seq": 2, "stream": "stderr", "data": "Receiving objects..."}
  ]
}
```

`status` progresses `pending` → `running` → `complete`. Messages are ordered by `seq` and preserve the interleaved order of stdout and stderr. See [exec commands doc](../docs/exec-commands.md) for the full schema.

### Images

```
GET /images           List all Drover-managed images
GET /images/{name}    Get image details
```

Drover discovers images by Docker labels rather than by tag prefix. Images must carry both of the following labels to appear in these listings:

| Label | Value |
|---|---|
| `drover.managed` | `"true"` |
| `drover.name` | short name used to reference the image (e.g. `"python-runner"`) |

`{name}` in `GET /images/{name}` is matched against the image's `drover.name` label. The returned `name` field on `ImageSummary` and `ImageDetail` is the value of that label, the `labels` field exposes the full `drover.*` label map (so callers can see flags like `drover.template`), and the `tags` field lists the image's Docker tags for informational use. Because labels are baked into the image, the same image can be pulled from any registry (for example `ghcr.io/saibotsivad/drover-builder:latest`) and the orchestrator will still recognise it.

## Container lifecycle

```
[*] → initializing → running ──────────────────────────────► destroying → destroyed
                        │                                         ▲
                        ├─► stopping → stopped → resuming ──┐    │
                        │                                   └─► running
                        └─► error → [*] (caller should DELETE to clean up)
```

States:

| Status | Meaning |
|---|---|
| `initializing` | Container created; waiting for guest agent to send `ready`. |
| `running` | Guest agent connected and ready; commands can be sent. |
| `stopping` | Stop requested; Docker stop in progress. |
| `stopped` | Paused; can be resumed. The socket file is preserved. |
| `resuming` | Docker start in progress after resume request. |
| `destroying` | Delete requested; Docker remove in progress. |
| `destroyed` | Terminal state; container and socket are gone. |
| `error` | Initialization failed. DB row kept for diagnostics; `DELETE` to remove. |

Error codes (set when status is `error`):

| `error_code` | Cause |
|---|---|
| `init_docker_error` | Docker create or start call failed. |
| `init_timeout` | Guest did not send `ready` within `DROVER_INIT_TIMEOUT_SECONDS`. |
| `orchestrator_crash` | Orchestrator restarted while container was initializing. |

A background reaper task runs every `REAPER_INTERVAL_SECONDS` and stops any running container whose `last_seen` timestamp is older than its `timeout_seconds`. Heartbeats from the guest agent update `last_seen`. A guest can also send `done` to request an immediate stop without waiting for the timeout.

See [container initialization](../docs/container-initialization.md) and [exec commands](../docs/exec-commands.md) for deeper detail.

## Authentication

Authentication is optional. When `DROVER_API_KEY` is set, all requests except `GET /health` must include:

```
Authorization: Bearer <token>
```

The server stores only the SHA-256 hash of the token; the plain-text token is never retained. Incoming tokens are hashed and compared with a constant-time HMAC comparison to prevent timing attacks.

When `DROVER_API_KEY` is unset the API is fully open — suitable for isolated homelabs, but add a network-level control (VPN, firewall rule) if the port is reachable externally.

The [webapp](../webapp/README.md) can hold the token and inject it into proxied requests, so end users do not need direct access to it.

## Socket protocol

The orchestrator and guest agents communicate over a per-container Unix socket using newline-delimited JSON. The socket is created in `SOCKET_DIR` before the container starts and passed to the container via a mount.

**Guest → Orchestrator:**

```jsonc
{"type": "ready"}                                               // initialization complete
{"type": "heartbeat"}                                           // keep-alive; updates last_seen
{"type": "output", "id": "<cmd_id>", "stream": "stdout", "data": "..."}
{"type": "result", "id": "<cmd_id>", "exit_code": 0}           // command finished
{"type": "done"}                                                // request immediate stop
```

**Orchestrator → Guest:**

```jsonc
{"type": "command", "id": "<cmd_id>", "exec": "git clone ..."}
```

The [executor](../executor/README.md) library implements this protocol for Python-based guest agents. For other languages or shells, write directly to the socket. See the main README for a minimal bash example.

## Database

The orchestrator uses SQLite (via aiosqlite) in WAL mode. The database is created automatically on first start if the file exists at `DB_PATH`.

**`containers`** — one row per container, retained after destruction for audit purposes.

**`commands`** — one row per `exec` invocation, with status (`pending` / `running` / `complete`) and exit code.

**`command_messages`** — one row per output chunk from a command, with stream (`stdout` / `stderr`), data, and an auto-increment `seq` for ordering.

Container IDs and command IDs are ULID-compatible: 26-character Crockford base32 strings that are lexicographically sortable by creation time.

## Logging

The orchestrator logs structured JSON to stdout, one object per line:

```json
{"timestamp": "2026-05-09T12:00:00Z", "level": "INFO", "logger": "orchestrator", "message": "..."}
```

Noisy third-party loggers (`uvicorn.access`, `httpx`, `httpcore`) are suppressed. Set `LOG_LEVEL=DEBUG` to see Docker API calls and socket traffic.

## Testing

Unit tests live in [`tests/`](../tests/) and use pytest-asyncio.

```sh
pytest tests/ -v
```

The CI workflow also runs a Docker build and a `GET /health` smoke test against the built image. See [`requirements-test.txt`](../requirements-test.txt) for test dependencies.

## Versioning and releases

The orchestrator is versioned independently from the other components. It is published to GHCR as `ghcr.io/saibotsivad/drover`.

To propose a version bump, add a YAML file to [`changes/`](../changes/):

```yaml
- project: orchestrator
  bump: minor
  description: |
    Short description of what changed.
```

The release workflow reads these files, updates `CHANGELOG.yml`, creates a release PR, and on merge pushes a `orchestrator-vX.Y.Z` git tag and publishes the image. See [versioning docs](../docs/versioning.md) for the full workflow.
