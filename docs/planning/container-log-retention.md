# Container Log Retention

> Plan for capturing, persisting, and serving the raw stdout/stderr of micro-containers across their lifecycle, while staying compatible with the operator's existing log-shipping tools.

---

## Goal

After this work is implemented, an operator can:

1. Inspect the full stdout/stderr history of any non-destroyed micro-container — including ones that were stopped hours or days earlier — through Drover's API and on disk.
2. Stream a live tail of a running container's stdout/stderr over the WebSocket endpoint planned in `websocket-streaming-plan.md`, with the same data the file is being written from.
3. Drop a Promtail / Vector / Fluent Bit / Loki agent on the log directory and have it ingest Drover micro-container logs without writing any Drover-specific code.
4. Lose the captured logs if and only if the container is destroyed (`DELETE /containers/{id}`). Stopping a container preserves its logs indefinitely; destroying it removes them along with the rest of the container's state.

The orchestrator's own logs and the per-command output (`command_messages` in SQLite) are out of scope — both already work and have their own retention semantics.

---

## Background

### What logs we mean

Drover handles two distinct streams:

| Stream | Source | Current persistence | Scope here |
|---|---|---|---|
| Per-command stdout/stderr | Guest agent → Unix socket → orchestrator | SQLite `command_messages` table | Out of scope |
| Container stdout/stderr | Docker daemon log capture for the container PID 1 | Lost when the container is removed | **In scope** |

The README is explicit that Docker's stdout from a micro-container is "unstructured debug output" with no semantic meaning to the orchestrator. That is exactly what makes it valuable for *human* diagnostics — and exactly what disappears today the moment you `DELETE /containers/{id}` (or, in some configurations, the moment Docker's log driver chooses to rotate).

### Prior decisions and adjacent work

- `docs/decisions/2026-04-11-websockets-for-streaming.md` — accepted; commits us to WebSocket as the streaming transport.
- `docs/planning/websocket-streaming-plan.md` — proposes a `/ws/containers/{id}/logs` endpoint that opens a Docker follow stream and forwards parsed lines. Phase 2 of that plan, currently scoped without persistence (open question 3 of that doc explicitly punts: *"Should container logs be persisted like command output? Decision: No, for now they are ephemeral."*).
- `orchestrator/docker_client.py:143` already has a non-streaming `get_container_logs()`; the streaming variant is on the WebSocket plan's TODO.
- `orchestrator/container_manager.py` is where the lifecycle hooks live (`_init_container`, `stop_container`, `resume_container`, `destroy_container`, `sync_containers`). Adding a sibling "log capture" lifecycle is a clean extension.

This plan **supersedes** the "logs are ephemeral" decision in the WebSocket plan and turns Phase 2 into a tee: the same Docker follow stream feeds both the WebSocket fan-out and a disk writer.

### Constraints

- **No required external stack.** A homelab operator should be able to run Drover with nothing else and still get useful log retention. Loki/Grafana/journald are options the operator may add, not preconditions.
- **No new heavy dependencies.** The orchestrator currently uses only FastAPI, httpx, aiosqlite, and uvicorn. Pulling in a logging framework (Vector, Fluent Bit) inside the orchestrator container is off the table.
- **Rootless Docker compatible.** The orchestrator runs as UID 1000 and only has access to what it owns. Reading Docker daemon files directly off the host filesystem is fragile under rootless and is avoided.
- **Survives orchestrator restart.** Logs and the capture process must be recoverable after the orchestrator container itself is stopped, restarted, or upgraded.

---

## Proposal

### Capture pipeline

For each container that reaches `running`, the orchestrator opens a single follow stream via the Docker API:

```
GET /containers/{docker_id}/logs?stdout=1&stderr=1&follow=1&timestamps=1&since=<last_ts>
```

The response is Docker's multiplexed stream (8-byte header per frame indicating stream and length, followed by the payload). A new `LogCaptureManager` parses this stream and tees each parsed `{stream, data, time}` chunk to two consumers:

1. **Disk writer:** appends one JSON line per chunk to the current open log file for that container.
2. **WebSocket fan-out** (from the WebSocket streaming plan, Phase 2): broadcasts to any connected `/ws/containers/{id}/logs` subscribers.

There is exactly one Docker follow stream per container, regardless of how many WebSocket subscribers are connected. The disk writer is always one of the consumers; it never goes away while the container is running.

### On-disk format

We adopt **Docker's `json-file` driver line format verbatim**:

```json
{"log":"Cloning into 'repo'...\n","stream":"stdout","time":"2026-05-05T12:34:56.789012345Z"}
```

One JSON object per line, newline-delimited. This is the format Promtail's `docker` pipeline stage, Vector's `docker_logs` source, Fluent Bit's `docker` parser, and Loki's docker-driver expect by default. Operators get free compatibility — if they already ship Docker's own json-file logs, they ship ours the same way.

We deliberately do **not** invent a Drover-specific schema. The operator is welcome to enrich (add labels, container metadata, etc.) in their log shipper using the directory structure as the source of metadata.

### Directory layout

```
{DROVER_LOG_DIR}/                         # default: /var/lib/orchestrator/logs
├── cnt_abc123/
│   ├── 0.log
│   ├── 1.log
│   └── 2.log                             # current writer always highest-numbered
└── cnt_def456/
    └── 0.log
```

- One directory per Drover container ID.
- Numerical filenames so that `ls`, `cat *.log`, and log shippers can read in chronological order without parsing names.
- Rotation by file size: when the current file exceeds `DROVER_LOG_MAX_FILE_BYTES` (default 10 MiB) on the next write, close it and open `{n+1}.log`.
- Retention: indefinite by default. If `DROVER_LOG_MAX_FILES_PER_CONTAINER` is set (default `0` = unlimited), the oldest files are deleted when the cap is exceeded.

### Lifecycle integration

| Container event | Log-capture action |
|---|---|
| `initializing` | No action. The container has no Docker ID yet, and there is nothing to read. |
| First entry into `running` (ready received) | Create `{DROVER_LOG_DIR}/{id}/`, open `0.log`, start the capture task. |
| `running` → `stopping` → `stopped` (any path: explicit stop, idle timeout, done signal) | Capture task observes the Docker follow stream closing and exits cleanly. Current log file is flushed and closed. Directory is retained. |
| `stopped` → `resuming` → `running` | Reopen the latest log file (or rotate to a new one if the previous file is over the size threshold), start a new capture task, request only logs `since=<last_recorded_ts>` so we resume without gaps or duplicates. |
| `running`/`stopped` → `destroying` → `destroyed` | After Docker confirms removal, `rm -rf {DROVER_LOG_DIR}/{id}/`. |
| Init failure → `error` | If the Docker container was created and started before the failure, capture and retain logs the same as a stopped container. If the container never started, no log directory is created. |
| Orchestrator restart (`sync_containers`) | For each row that ended up in `running`: restart the capture task with `since=<last_recorded_ts>` to bridge the gap. For `stopped`: nothing to do. For `destroyed`: confirm directory is gone; if not, remove it. |

The "track the last timestamp per container" requirement is handled by the disk writer recording the timestamp of the most recently written chunk in memory and persisting it to a small per-container metadata file (`{DROVER_LOG_DIR}/{id}/.cursor`). On startup we read `.cursor` and use it as the `since=` parameter. If `.cursor` is missing we use `since=0` and accept potential duplicates with whatever was on disk (small price for the rare crash recovery case).

### API surface

Two endpoints, both following the existing patterns:

**Existing endpoint, semantics broadened:**

```
GET /containers/{id}/logs?since=<rfc3339>&until=<rfc3339>&tail=<n>
```

Today this proxies to Docker's logs endpoint. After this change, it reads from the on-disk capture for any time range — including ranges entirely after the container stopped — and only falls back to Docker for live tailing of a currently running container when no `since`/`until` are given. Behavior remains compatible for current callers.

Response is JSON: `{"messages": [{"stream": "stdout|stderr", "data": "...", "time": "..."}, ...]}`, paginated.

**New WebSocket (handled in the WebSocket streaming plan, Phase 2):**

```
GET /ws/containers/{id}/logs?tail=<n>&follow=true
```

Phase 2 of `websocket-streaming-plan.md` already specifies this. The only adjustment from this plan is that `tail=<n>` is now served from disk (so it works for stopped containers) rather than from Docker.

### Configuration

Three new env vars, all optional:

| Variable | Default | Purpose |
|---|---|---|
| `DROVER_LOG_DIR` | `/var/lib/orchestrator/logs` | Root directory for captured micro-container logs. |
| `DROVER_LOG_MAX_FILE_BYTES` | `10485760` (10 MiB) | Rotate to a new file when this size is exceeded on the next write. |
| `DROVER_LOG_MAX_FILES_PER_CONTAINER` | `0` (unlimited) | If non-zero, oldest log files are deleted to keep the count at or below this limit. |

Mount: the orchestrator's `docker-compose.yml` example gains a volume mount for `DROVER_LOG_DIR` so logs survive orchestrator restarts. This is the only deployment change.

### External-tool compatibility

The "compatible with Docker, Loki, etc." goal is met by *not integrating with them at all* — instead by being a well-behaved file producer that any of them can consume.

- **Docker log driver:** Whatever the operator has configured at the daemon level (`json-file`, `journald`, `syslog`, `fluentd`, `gelf`, `loki`) continues to apply to the micro-container Docker stdout, totally independent of what Drover does. Drover does not set per-container log drivers and does not interfere with the daemon's choice. Operators who already pipe Docker logs to Loki keep doing so.
- **Drover's own captured files:** because they match `json-file` line format, all of these tools recognize them out of the box. Documentation will include a Promtail snippet (the canonical case) showing how to add `DROVER_LOG_DIR` as a `static_configs` target.
- **Orchestrator-level structured logs:** unchanged. The orchestrator already emits one JSON object per line to its own stdout (`orchestrator/app.py:24`); operators ship those via their normal Docker logging path.

The result: a homelab operator with nothing installed has working log retention via REST and WebSocket. An operator with Loki gets the same retention plus their dashboards, by adding one scrape config. Neither operator has to install or run anything they didn't already want.

---

## Alternatives Considered

### A. Read Docker's `json-file` files directly off the host

**Approach:** Mount `/var/lib/docker/containers/` (or the rootless equivalent) read-only into the orchestrator and read `<docker_id>/<docker_id>-json.log` directly.

**Why rejected:**

- Couples Drover to a specific Docker log driver. If the operator has set `--log-driver=journald` (common), the file does not exist.
- Rootless Docker stores under `~/.local/share/docker/`; the path varies by user and is not guaranteed to be readable by UID 1000 inside the orchestrator container.
- Docker deletes those files on `docker rm`, which is exactly the point at which we still want them — so it does not solve retention without copying out anyway.
- Bypasses the Docker API in favor of filesystem layout that is technically internal.

### B. Snapshot logs via the non-streaming `docker logs` API only at stop time

**Approach:** Skip live capture. When a container transitions to `stopped`, call `GET /containers/{id}/logs?stdout=1&stderr=1` once, dump the result to disk, done.

**Why rejected:**

- Loses the live-tail use case. The WebSocket logs endpoint still has to open its own follow stream, doubling work.
- A long-running container with verbose output may exhaust Docker's own log driver buffer (default 10 MiB rotated × 5 with `json-file`) before we read it. Earlier output is gone by the time we snapshot.
- An OOM-killed or `kill -9`'d container that the orchestrator missed produces partial logs (Docker may have already rotated).
- The work saved over option C is small: streaming and writing to a file is not significantly more code than streaming and writing to a websocket, and we have to do the streaming for the WebSocket plan anyway.

### C. Force every micro-container to use Docker's `--log-driver=local` and shell out to `docker logs --tail`

**Approach:** Reuse Docker's own retention and rotation; treat Drover as a thin wrapper around `docker logs`.

**Why rejected:**

- Same retention problem: `docker rm` deletes the logs. We still need to copy them out before destroy, so we still need a capture path.
- Forcing a log driver fights any operator who has globally configured a different one (e.g. ships everything to Loki via the loki driver). Drover should not override the host Docker daemon's policy.
- Removes the operator's freedom to fan their micro-container Docker logs out to their own stack, which is one of the explicit goals.

### D. Build a "Drover ships with Loki/Grafana" stack

**Approach:** Provide a docker-compose profile with Loki, Promtail, and Grafana wired up; recommend it as the supported logging path.

**Why rejected:**

- Demands the operator install and maintain a logging stack they may not want, just to get basic log retention. Drover is meant to work standalone in a homelab.
- The decision to use Loki versus Grafana Cloud versus self-hosted Elasticsearch versus journald-only is operator-specific. Hard-wiring an opinion ages poorly.
- We can still document the Loki path *as an example* without bundling it.

The preferred approach (in the Proposal) is the union of (B) and the WebSocket plan's Phase 2: stream once from Docker, tee to disk and to subscribers. It costs us a single long-lived task per running container and a small amount of disk per container.

---

## Key Decisions

These are the choices baked into the plan that should drive lower-level decisions during implementation.

1. **The on-disk format is exactly Docker's `json-file` driver line format.** Not a Drover-specific schema, not multiplexed binary, not pretty-printed. One JSON object per line with keys `log`, `stream`, `time`. This is the single biggest interoperability lever in the plan and we should not deviate from it.

2. **One Docker follow stream per running container, multiplexed in process.** The disk writer and WebSocket fan-out share a single stream. Adding a third consumer later (e.g. a metrics collector) is in-process work, not another Docker connection.

3. **Retention is binary: kept until destroy, then gone.** No TTL, no time-based pruning by default. This matches user mental model: "the container exists → its logs exist." Time-based retention can be added later if requested without changing data layout.

4. **Rotation is by file size with a per-container file count cap, both opt-in.** Default is 10 MiB rotation, unlimited file count. Operators with bounded disk can set a cap. We do not implement compression; if disk pressure is a concern, the operator's log shipper can ship and drop, or they can mount the directory on compressed storage.

5. **Resume semantics use Docker's `since=` parameter against a per-container `.cursor` file.** Avoids both gaps (after orchestrator restart) and duplicates (after orchestrator restart) within the precision Docker offers. If the cursor is corrupted or missing, we accept possible duplicates rather than possible gaps; logs are easier to dedupe than to recover.

6. **The orchestrator does not own the operator's log driver choice.** Drover never sets `HostConfig.LogConfig` on container creation. Operators retain full control of where Docker itself sends container output.

---

## Open Questions

1. **Should we offer a "passthrough" mode that skips disk capture when the operator has configured `--log-driver=json-file` and is happy with Docker's retention?** This would avoid double-writing for operators who already have Docker writing to disk. The ergonomic cost is that it changes Drover's persistence guarantees based on host configuration that Drover does not directly observe. **Tentative answer:** no, not in v1. Always capture. Revisit if disk usage proves to be a real complaint.

2. **Should rotation also be time-based (e.g. roll daily)?** Time-based rotation is friendlier for log shippers that index by date. Size-based is friendlier for chatty short-lived containers. **Tentative answer:** size-only in v1. Add time-based as an additive option later if asked.

3. **What happens if disk fills up?** Options: (a) drop new log writes silently, container keeps running; (b) stop accepting new logs and emit an orchestrator-level error log; (c) destroy the affected container. **Tentative answer:** (b) — log a single error per container per minute, drop subsequent writes for that container until disk recovers. The operator's monitoring should already alert on disk pressure.

4. **Per-container log isolation between API keys?** Today the API has no concept of ownership. If multi-tenant ever lands, log directory permissions need revisiting. **Tentative answer:** out of scope; revisit when auth gets richer than a single API key.

5. **Encryption at rest?** Some homelab operators may be running Drover on shared storage. **Tentative answer:** out of scope; storage-layer encryption is the right place for this, not the application.

6. **`GET /containers/{id}/logs` pagination contract.** The WebSocket plan calls out the existing endpoint but does not specify a pagination cursor for large historical reads. We should pick one (offset + limit, or `since`/`until` time bounds, or opaque cursor) and apply consistently. **Tentative answer:** `since` + `until` time bounds plus a `limit` ceiling, with the response returning the timestamp of the next record after the cap. Tracks the data layout (timestamps are first-class in the on-disk format) and matches Docker's own log query model.

---

## Implementation Notes

Enough to break this into tickets. Not exhaustive.

### New module: `orchestrator/log_capture.py`

A `LogCaptureManager` class, owned by the lifespan in `app.py` alongside `SocketManager` and `ContainerManager`.

Responsibilities:

- For a given Drover container ID and Docker container ID, open and own the follow stream against Docker's logs API.
- Parse the multiplexed Docker stream format (8-byte header `[stream_type][0][0][0][len:4]`, then `len` bytes of payload). Yield `(stream, data, time)` chunks.
- Append each chunk as a JSON line to the current open log file. Rotate when size exceeds the configured threshold; prune when the file count cap is exceeded.
- Update the in-memory `last_ts` and persist it to `.cursor` (atomic write: temp-file + rename, no fsync per write, fsync per rotation).
- Fan out parsed chunks to subscribers — this is where the WebSocket connection manager (from the WebSocket streaming plan) plugs in. The exact interface (queue, callback, observable) is shared with that plan; pick whichever shape that plan settles on.
- Provide a `read_range(container_id, since, until, limit)` method that the REST `GET /logs` endpoint calls.
- Provide `start(container_id, docker_id, since=None)`, `stop(container_id)`, and `discard(container_id)` methods called by `ContainerManager` on lifecycle transitions.

### Changes to existing modules

- **`orchestrator/config.py`:** add the three new env vars.
- **`orchestrator/docker_client.py`:** add `stream_container_logs(container_id, *, since: float | None, follow: bool) -> AsyncIterator[bytes]` that opens the follow stream and yields raw bytes. The multiplex parsing belongs in `log_capture.py`, not here.
- **`orchestrator/container_manager.py`:** call into `LogCaptureManager` from the lifecycle methods listed in the table above. Pass the docker ID after Docker create completes; pass the cursor on resume.
- **`orchestrator/routers/containers.py`:** broaden `GET /containers/{id}/logs` to read from `LogCaptureManager.read_range(...)` for time-bounded queries, falling back to the live stream only when no bounds are supplied and the container is running.
- **`orchestrator/app.py`:** instantiate `LogCaptureManager` in `lifespan`, call its shutdown method on app exit (closes all active capture streams, flushes files).
- **`docker-compose.yml`:** mount `/var/lib/orchestrator/logs` as a volume.
- **`README.md`:** document the new env vars, the on-disk format, and the Promtail example.
- **`docs/planning/websocket-streaming-plan.md`:** update Phase 2 and Open Question 3 to reference this plan; the `/ws/.../logs` endpoint reads from the shared `LogCaptureManager` and the `tail=<n>` parameter is served from disk.
- **`TODO.md`:** remove the "Container log retention" section once this is implemented; add a follow-up note for the open questions if any remain unanswered at landing.

### Tests

- Unit test the multiplex parser against synthetic Docker streams (interleaved stdout/stderr, partial frames spanning chunk boundaries, zero-length frames).
- Unit test rotation: write past threshold, assert new file opens at next write.
- Unit test cap: with `MAX_FILES_PER_CONTAINER=3`, rotate enough to trigger pruning, assert oldest is deleted.
- Unit test cursor: write some chunks, simulate restart, assert resume uses the recorded `since`.
- Integration test against the orchestrator stack (similar to existing `tests/`): create container, run a chatty workload, stop, resume, query historical logs, destroy, verify directory is gone.

### Sequencing

1. Land `LogCaptureManager` (capture, rotation, on-disk format, no API surface yet) and unit tests.
2. Wire into `ContainerManager` lifecycle. Now logs land on disk and are deleted on destroy. WebSocket and REST endpoints are unchanged.
3. Plug into the WebSocket fan-out as part of the WebSocket plan Phase 2.
4. Broaden the REST `GET /logs` endpoint to serve from disk.
5. Documentation pass (README, docker-compose, ADR for the on-disk-format decision).

Steps 1–2 are independently shippable and useful. Steps 3–4 depend on the WebSocket plan landing or moving in parallel.

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Disk usage grows unboundedly for long-running noisy containers. | Rotation by size with optional file-count cap. Operator monitoring of `DROVER_LOG_DIR` is the safety net. Document expected disk usage in the README. |
| Orchestrator restart causes gap or duplication in captured logs. | `.cursor` file with `since=` resume. Documented behavior under crash recovery: prefer dupes to gaps. |
| Docker daemon's own log driver buffer is smaller than our capture rate, so logs are lost before we read them. | We open a follow stream the moment the container reaches `running`; Docker streams in real time as long as the connection is held. The capture task is high-priority and not gated on anything else. If the asyncio event loop is blocked, both Drover and the WebSocket fan-out are equally affected — this is an orchestrator-health concern, not a logging concern. |
| Filesystem semantics on the host (e.g. SMB-mounted log dir) cause partial writes. | Append-only writes with newline terminator; partial writes look like a truncated last line, which all log shippers handle. Document a recommendation to keep `DROVER_LOG_DIR` on local storage. |
| Operator's existing log shipper picks up our files *and* Docker's `json-file` files for the same container, producing duplicates downstream. | Document this clearly. The shipper's own dedupe (or path filtering) is the right place to handle it. The two paths exist for different reasons — Docker's path is the operator's stack; our path is Drover's retention. |
| Permissions: orchestrator runs as UID 1000; if the log volume is owned differently the capture fails on first write. | The orchestrator already creates `/var/lib/orchestrator` with the right ownership; the same Dockerfile step covers `/var/lib/orchestrator/logs`. Document the ownership requirement for operator-supplied volumes. |

Rollback: this work is additive and isolatable. If something goes wrong in production, the operator can set `DROVER_LOG_MAX_FILE_BYTES=0` (treated as "disable capture", to be implemented) or simply not mount the log volume — the orchestrator continues to function exactly as it does today, minus retention. Reverting the code is also low-risk because no existing data structures change.

---

## Documentation Impact

Files that need updates when this lands:

- `README.md` — new env vars table, new mounts row in the orchestrator container section, brief subsection on the on-disk format and a Promtail example.
- `docker-compose.yml` — add `drover-logs` volume and mount it on the orchestrator service.
- `TODO.md` — remove the "Container log retention" section.
- `docs/planning/websocket-streaming-plan.md` — update Phase 2 to describe the tee, and update Open Question 3 to point to this plan.
- `docs/decisions/` — new ADR capturing the on-disk-format decision (Docker `json-file` line format) since that is the kind of decision a future engineer might reasonably want to revisit and needs to know the reasoning for.

Possibly worth a separate ADR on retention semantics ("logs live until the container is destroyed") if it turns out to be load-bearing for later product decisions; not strictly required for the v1 implementation.
