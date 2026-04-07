# Drover Orchestrator — Implementation Plan

This plan covers building the orchestrator described in `README.md`. The existing `orchestrator/` folder contains a stub FastAPI app and Dockerfile used to validate the GHCR publish workflow. We'll replace that with the real implementation.

**Guiding principles:** minimal dependencies, modern Python (3.12+), async throughout, no ORM — just raw SQLite via `aiosqlite`.

## Dependencies

| Package | Purpose |
|---|---|
| `fastapi` | REST API framework |
| `uvicorn` | ASGI server |
| `aiosqlite` | Async SQLite access |
| `httpx` | HTTP client for Docker Engine API over Unix socket |

No Docker SDK — we talk to the Docker daemon directly via its REST API over the mounted `/var/run/docker.sock` using `httpx` with a Unix transport. This keeps the dependency tree small and gives us full control.

---

## Phase 1 — Project Skeleton & Configuration

Set up the project structure, configuration loading, and application entrypoint.

```
orchestrator/
├── app.py              # FastAPI app factory, lifespan, middleware
├── config.py           # Env var loading (PRIVILEGED_IMAGE, paths, timeouts)
├── database.py         # SQLite connection, schema init, migrations
├── models.py           # Pydantic models (request/response schemas)
├── docker_client.py    # Thin async wrapper around Docker Engine API
├── socket_manager.py   # Unix socket lifecycle & message routing
├── container_manager.py# Container lifecycle orchestration
├── routers/
│   ├── images.py       # /images endpoints
│   └── containers.py   # /containers endpoints
├── Dockerfile
└── requirements.txt
```

- [ ] Define project layout and create empty modules
- [ ] Implement `config.py` — read `PRIVILEGED_IMAGE`, `DB_PATH`, `SOCKET_DIR`, `DOCKER_SOCK` from env with sensible defaults
- [ ] Implement `database.py` — async context manager for DB connection, `init_db()` that creates tables on startup
- [ ] Define SQLite schema: `containers` table (id, docker_id, image, privileged, status, socket_path, label, timeout_seconds, last_seen, created_at, stopped_at)
- [ ] Implement `models.py` — Pydantic models for API request/response bodies and internal container state
- [ ] Wire up `app.py` — FastAPI lifespan that initializes DB, starts background tasks, and cleans up on shutdown
- [ ] Keep existing `/health` endpoint working throughout

## Phase 2 — Docker Engine Client

Build a thin async client that talks to the Docker daemon over its Unix socket.

- [ ] Implement `docker_client.py` with an `httpx.AsyncClient` using a Unix socket transport to `/var/run/docker.sock`
- [ ] `list_images(prefix)` — `GET /images/json` filtered to `drover/*` references
- [ ] `inspect_image(name)` — `GET /images/{name}/json`
- [ ] `create_container(config)` — `POST /containers/create` with image, env, binds, runtime, etc.
- [ ] `start_container(id)` — `POST /containers/{id}/start`
- [ ] `stop_container(id)` — `POST /containers/{id}/stop`
- [ ] `remove_container(id)` — `DELETE /containers/{id}`
- [ ] `inspect_container(id)` — `GET /containers/{id}/json`
- [ ] `get_container_logs(id, tail)` — `GET /containers/{id}/logs`
- [ ] Add error handling — translate Docker API errors into meaningful exceptions

## Phase 3 — Image API

Expose image listing and inspection through the REST API.

- [ ] Implement `routers/images.py`
- [ ] `GET /images` — list all `drover/*` images via `docker_client.list_images`, return name, tags, size, created date
- [ ] `GET /images/{name}` — inspect a specific `drover/{name}` image, return metadata and status
- [ ] Register image router in `app.py`
- [ ] Add tests for image endpoints

## Phase 4 — Container Lifecycle (without socket protocol)

Implement create, stop, resume, destroy — everything except command execution. This phase gets containers running and tracked in SQLite.

- [ ] Implement `container_manager.py` — orchestration layer between API, DB, and Docker client
- [ ] `create_container()` — validate image exists, create Unix socket path, build Docker container config (mounts, runtime, env), create & start via Docker API, insert DB row, return container metadata
- [ ] For standard containers: use `drover/{image}` image, add `--runtime=runsc`, mount only orchestrator socket
- [ ] For privileged containers: use `PRIVILEGED_IMAGE`, skip gVisor, mount Docker socket too
- [ ] Reject privileged requests when `PRIVILEGED_IMAGE` is unset
- [ ] `get_container(id)` — read from DB, optionally sync with Docker state
- [ ] `stop_container(id)` — call Docker stop, update DB status to `stopped`
- [ ] `resume_container(id)` — call Docker start, update DB status to `running`
- [ ] `destroy_container(id)` — call Docker stop + remove, update DB status to `destroyed`
- [ ] Implement `routers/containers.py` with all endpoints from the README
- [ ] Register container router in `app.py`
- [ ] Add tests for lifecycle transitions and error cases

## Phase 5 — Unix Socket Protocol & Command Execution

Implement the per-container Unix socket, the newline-delimited JSON protocol, and the `/exec` endpoint.

- [ ] Implement `socket_manager.py` — manages creation and cleanup of Unix sockets under `/var/run/microcontainers/`
- [ ] On container creation: create a Unix socket at `{SOCKET_DIR}/{container_id}.sock`, start an asyncio listener
- [ ] On guest connect: accept connection, begin reading newline-delimited JSON messages
- [ ] Handle inbound message types from container: `heartbeat`, `output`, `result`
- [ ] On `heartbeat`: update `last_seen` in DB
- [ ] On `output`: buffer/forward stream data (stdout/stderr) associated with a command ID
- [ ] On `result`: record exit code, mark command as complete
- [ ] Implement command sending: write `{"type": "command", "id": "...", "exec": "..."}` to socket
- [ ] Wire `POST /containers/{id}/exec` to send command via socket and return command ID
- [ ] Decide on response streaming for exec output (start with simple polling: `GET /containers/{id}/exec/{cmd_id}` returns buffered output — streaming can come later as noted in README open issues)
- [ ] On container stop/destroy: close socket connection, clean up socket file
- [ ] Add tests for socket protocol message parsing and routing

## Phase 6 — Timeout & Auto-Stop

Implement the background idle-timeout reaper.

- [ ] Add background task in app lifespan that runs on a configurable interval (e.g. every 30s)
- [ ] Query DB for all `running` containers where `now - last_seen > timeout_seconds`
- [ ] Stop timed-out containers via `container_manager.stop_container()`
- [ ] Log timeout events
- [ ] Handle edge cases: container already stopped externally, DB/Docker state drift
- [ ] Add tests for timeout logic

## Phase 7 — Dockerfile & Packaging

Replace the stub Dockerfile with a production build.

- [ ] Write `requirements.txt` (pinned versions)
- [ ] Write production `Dockerfile` — multi-stage build, non-root user (UID 1000), minimal image
- [ ] Expose port 8000, set `CMD` to run uvicorn
- [ ] Ensure GHCR workflow still works with new Dockerfile context
- [ ] Document required host mounts and env vars

## Phase 8 — Hardening & Open Issues

Address remaining quality and README open issues.

- [ ] Add structured logging throughout (Python `logging`, JSON format)
- [ ] Container log retention — store/serve Docker logs via API endpoint even after container destruction
- [ ] Validate all API inputs rigorously (image names, timeout ranges, etc.)
- [ ] Handle orchestrator restart gracefully — reconcile DB state with running Docker containers on startup
- [ ] Add rate limiting or basic auth placeholder (README lists auth as open issue)
- [ ] Write integration test scaffolding that can run with a real Docker socket

---

## Open Design Decisions

These are called out in the README and don't need to be resolved upfront, but should be addressed before v1:

1. **Command output streaming** — Phase 5 starts with polling. SSE or WebSocket can be added later.
2. **Auth** — No auth defined yet. Phase 8 adds a placeholder.
3. **Container-to-orchestrator message schema** — Phase 5 implements the types from the README examples; the exact schema will solidify as we build.
4. **Container "ready to delete" signal** — The container should be able to signal it's done. This can be a new message type added in Phase 5.
5. **Container API data model** — The exact response shapes will be defined in Phase 1 (models.py) and refined as we go.
