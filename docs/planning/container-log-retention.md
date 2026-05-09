# Container Log Retention

> Plan for capturing, persisting, and serving the raw stdout/stderr of micro-containers across their lifecycle, while staying compatible with the operator's existing log-shipping tools.

---

## Goal

After this work is implemented, an operator can:

1. Inspect the full stdout/stderr history of any non-destroyed micro-container — including ones that were stopped hours or days earlier — by reading the on-disk capture directory directly. The sample `docker-compose.yml` ships with this enabled.
2. Drop a Promtail / Vector / Fluent Bit / Loki agent on the log directory and have it ingest Drover micro-container logs without writing any Drover-specific code.
3. Lose the captured logs if and only if the container is destroyed (`DELETE /containers/{id}`). Stopping a container preserves its logs indefinitely; destroying it removes them along with the rest of the container's state.
5. Opt out of Drover-managed retention entirely by not setting `DROVER_LOG_DIR`, in which case Drover does no disk writing and historical log queries fall through to whatever Docker's configured log driver provides.

The orchestrator's own logs and the per-command output (`command_messages` in SQLite) are out of scope — both already work and have their own retention semantics.

---

## Background

### What logs we mean

Drover handles two distinct streams:

| Stream | Source | Current persistence | Scope here |
|---|---|---|---|
| Per-command stdout/stderr | Guest agent → Unix socket → orchestrator | SQLite `command_messages` table | Out of scope |
| Container stdout/stderr | Docker daemon log capture for the container PID 1 | Lost when the container is removed | **In scope** |

The Docker stdout from a micro-container is unstructured debug output with no semantic meaning to the orchestrator, but that is exactly what makes it valuable for *human* diagnostics, and currently disappears the moment you `DELETE /containers/{id}` (or, in some configurations, the moment Docker's log driver chooses to rotate).

### Prior decisions and adjacent work

- `docs/planning/websocket-streaming-plan.md` — proposes a `/ws/containers/{id}/logs` endpoint that opens a Docker follow stream and forwards parsed lines. Phase 2 of that plan, currently scoped without persistence (open question 3 of that doc explicitly punts: *"Should container logs be persisted like command output? Decision: No, for now they are ephemeral."*).
- `orchestrator/docker_client.py:143` already has a non-streaming `get_container_logs()`; the streaming variant is on the WebSocket plan's TODO.
- `orchestrator/container_manager.py` is where the lifecycle hooks live (`_init_container`, `stop_container`, `resume_container`, `destroy_container`, `sync_containers`). Adding a sibling "log capture" lifecycle is a clean extension.

This plan **supersedes** the "logs are ephemeral" decision and feeds the Docker follow stream to a disk writer.

**Important** - Implementing WebSockets is **out of scope** for this plan.

### Constraints

- **No required external stack.** A homelab operator should be able to run Drover with nothing else and still get useful log retention. Loki/Grafana/journald are options the operator may add, not preconditions.
- **No new heavy dependencies.** The orchestrator currently uses only FastAPI, httpx, aiosqlite, and uvicorn. Pulling in a logging framework (Vector, Fluent Bit) inside the orchestrator container is off the table.
- **Rootless Docker compatible.** The orchestrator runs as UID 1000 and only has access to what it owns. Reading Docker daemon files directly off the host filesystem is fragile under rootless and is avoided.
- **Survives orchestrator restart.** Logs and the capture process must be recoverable after the orchestrator container itself is stopped, restarted, or upgraded.

---

## Proposal

### Capture pipeline

For each container that reaches `running`, **if** `DROVER_LOG_DIR` is set the orchestrator opens a single follow stream via the Docker API:

```
GET /containers/{docker_id}/logs?stdout=1&stderr=1&follow=1&timestamps=1&since=<last_ts>
```

The response is Docker's multiplexed stream (8-byte header per frame indicating stream and length, followed by the payload).

A new `LogCaptureManager` parses this stream and feeds each parsed `{stream, data, time}` chunk to a disk writer which appends one JSON line per chunk to the current open log file for that container.

The disk writer is an opt-in consumer: when `DROVER_LOG_DIR` is unset, the follow stream is not created, no directory is created, and no `.cursor` file is maintained.

Two operating modes follow from this:

- **Drover-managed retention (`DROVER_LOG_DIR` set, sample compose default):** historical logs survive container stop and are removed only on destroy. Operators reach historical logs by reading the directory directly (or via a log shipper pointed at it). This is the recommended mode for a homelab without an existing log pipeline.
- **Ignored (`DROVER_LOG_DIR` unset):** Drover writes nothing to disk and adds no retention guarantees of its own. Historical queries fall through to Docker's logs API via the existing (unchanged) `GET /containers/{id}/logs` endpoint, so retention is whatever Docker's configured log driver provides. This is the recommended mode for operators who already ship Docker container logs to Loki/Elastic/journald and want a single source of truth.

### On-disk format

We adopt **Docker's `json-file` driver line format verbatim**:

```json
{"log":"Cloning into 'repo'...\n","stream":"stdout","time":"2026-05-05T12:34:56.789012345Z"}
```

One JSON object per line, newline-delimited. This is the format Promtail's `docker` pipeline stage, Vector's `docker_logs` source, Fluent Bit's `docker` parser, and Loki's docker-driver expect by default. Operators get free compatibility — if they already ship Docker's own json-file logs, they ship ours the same way.

We deliberately do **not** invent a Drover-specific schema. The operator is welcome to enrich (add labels, container metadata, etc.) in their log shipper using the directory structure as the source of metadata.

### Directory layout

```
{DROVER_LOG_DIR}/                         # sample compose: /var/lib/orchestrator/logs
├── cnt_abc123/
│   ├── 0.log
│   ├── 1.log
│   ├── 2.log                             # current writer always highest-numbered
│   └── .cursor                           # last-written timestamp for resume
└── cnt_def456/
    ├── 0.log
    └── .cursor
```

- One directory per Drover container ID.
- Numerical filenames so that `ls`, `cat *.log`, and log shippers can read in chronological order without parsing names.
- Rotation by file size: when the current file exceeds `DROVER_LOG_MAX_FILE_BYTES` (default 10 MiB) on the next write, close it and open `{n+1}.log`.
- Retention: indefinite until destroy.

### Lifecycle integration

Log capture is bound to the existence of a started Docker container, not to Drover's `running` status. The window between Docker `start` and Drover `running` (i.e. waiting for the guest agent's `ready` message) is when init failures emit the most diagnostically useful output, so capture must already be active during that window.

| Container event | Log-capture action |
|---|---|
| `initializing`, before Docker `create` returns | No action. There is nothing to read yet. |
| `initializing`, immediately after `docker.start_container()` succeeds in `_init_container` | Start the capture task using the freshly-known Docker ID. If `DROVER_LOG_DIR` is set, also create `{DROVER_LOG_DIR}/{id}/` and open `0.log`. **This happens before the `ready` message and covers the init-failure window.** |
| `initializing` → `running` (ready received) | No log-layer action; the capture task is already running. The status transition is purely a Drover-level concern. |
| `initializing` → `error` (init timeout, init Docker error, orchestrator crash mid-init) | The capture task either is already running (Docker started successfully but `ready` never arrived) or was never started (Docker create/start itself failed). If running, it observes Docker stop/remove triggered by `_fail_init` and exits cleanly; the directory is retained so the operator can post-mortem the init failure. If never started, there is no directory. The directory is removed only when the operator subsequently destroys the errored container. |
| `running` → `stopping` → `stopped` (explicit stop, idle timeout, or done signal) | Capture task observes the Docker follow stream closing and exits cleanly. If a disk writer was attached, flush and close the current file. Directory is retained. |
| `stopped` → `resuming` → `running` | Start a new capture task. If a disk writer is attached, reopen the latest log file (or rotate to a new one if the previous file is over the size threshold) and request only logs `since=<last_recorded_ts>` so we resume without gaps or duplicates. In capture-only mode, request from `since=0` (no cursor exists) and accept that the follow stream catches up to the present quickly. |
| any non-destroyed status → `destroying` → `destroyed` | After Docker confirms removal, if a log directory exists, `rm -rf {DROVER_LOG_DIR}/{id}/`. In capture-only mode, no-op. This includes destroying an `initializing` or `error` container — the partial init logs go away with the rest of the container's state. |
| Orchestrator restart (`sync_containers`) | For each row in `running` or `initializing` whose Docker container is still up: restart the capture task. With a log dir, use `since=<last_recorded_ts>` from `.cursor` to bridge the gap (or `since=0` if no cursor exists yet, which is the common case for a still-initializing container that crashed before its first log line was written). Without a log dir, simply re-attach a fresh follow stream. For `stopped`: nothing to do. For `destroyed` (with a log dir): confirm directory is gone; if not, remove it. Containers that get force-failed to `error` by the post-reconciliation pass have their directories preserved like any other `error` row. |

The "track the last timestamp per container" requirement is handled by the disk writer recording the timestamp of the most recently written chunk in memory and persisting it to a small per-container metadata file (`{DROVER_LOG_DIR}/{id}/.cursor`). On startup we read `.cursor` and use it as the `since=` parameter. If `.cursor` is missing we use `since=0` and accept potential duplicates with whatever was on disk (small price for the rare crash recovery case). In capture-only mode there is no cursor, so the only correctness guarantee is that the follow stream eventually reaches the live tail.

### API surface

This plan changes only one external surface: the WebSocket logs endpoint planned in `websocket-streaming-plan.md` now reads from the shared `LogCaptureManager` rather than opening its own Docker stream. The existing REST endpoint is unchanged in v1.

**Existing endpoint — unchanged:**

```
GET /containers/{id}/logs
```

Continues to proxy Docker's logs API exactly as it does today. When `DROVER_LOG_DIR` is set, captured logs are written to disk in parallel but are not exposed through this endpoint in v1. Operators who want historical-log API access wait for the follow-up pagination plan, which will broaden this endpoint with a consistent `since`/`until`/`limit`/`offset` contract shared across all list endpoints. See Follow-Up Work.

In the meantime, operators have two ways to access retained logs of stopped containers:

1. Read the on-disk capture directly (mounted volume).
2. Run a standard log shipper (Promtail, Vector, etc.) against the capture directory.

**WebSocket (handled in the WebSocket streaming plan, Phase 2):**

```
GET /ws/containers/{id}/logs?tail=<n>&follow=true
```

Phase 2 of `websocket-streaming-plan.md` specifies this. The capture pipeline in this plan replaces that endpoint's plan to open its own Docker stream — the WebSocket fan-out is now a consumer of the shared `LogCaptureManager`. `tail=<n>` is forwarded to Docker as the `?tail=N` query parameter at follow-stream-open time in both modes; tail-from-disk is also a feature deferred to the pagination plan.

### Configuration

Two new env vars, all optional:

| Variable | Default | Purpose |
|---|---|---|
| `DROVER_LOG_DIR` | _(unset; the sample compose stack sets it)_ | Root directory for captured micro-container logs. **When unset, Drover runs in capture-only mode**: the follow stream is opened (so live WebSocket tailing works) but nothing is written to disk and no retention is provided. When set, Drover-managed retention is enabled. |
| `DROVER_LOG_MAX_FILE_BYTES` | `10485760` (10 MiB) | Rotate to a new file when this size is exceeded on the next write. Ignored when `DROVER_LOG_DIR` is unset. |

Mount: the sample `docker-compose.yml` ships with `DROVER_LOG_DIR=/var/lib/orchestrator/logs`, which lives inside the existing `drover-data` volume so logs survive orchestrator restarts without a second volume to manage. The compose file carries an inline comment noting that this roughly doubles per-container disk usage (Docker's own log driver also keeps a copy by default) and points the operator at `docs/observability.md` for guidance on disabling one or the other if disk is tight.

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

3. **Retention is binary: kept until destroy, then gone.** When Drover-managed retention is enabled, no TTL, no time-based pruning by default. This matches user mental model: "the container exists → its logs exist." Time-based retention can be added later if requested without changing data layout. When retention is disabled (capture-only mode), Drover provides no retention guarantee at all and operators are expected to use Docker's log driver for that.

4. **Rotation is by file size.** Default is 10 MiB per file. We do not implement compression; if disk pressure is a concern, the operator's log shipper can ship and drop, or they can mount the directory on compressed storage.

5. **Resume semantics use Docker's `since=` parameter against a per-container `.cursor` file.** Avoids both gaps (after orchestrator restart) and duplicates (after orchestrator restart) within the precision Docker offers. If the cursor is corrupted or missing, we accept possible duplicates rather than possible gaps; logs are easier to dedupe than to recover.

6. **The orchestrator does not own the operator's log driver choice.** Drover never sets `HostConfig.LogConfig` on container creation. Operators retain full control of where Docker itself sends container output.

7. **Rotation is size-only; no time-based rotation.** Adding daily/hourly rotation requires a scheduler (or per-write clock check) that we don't otherwise need, and it complicates the lifecycle for short-lived containers that may live for less than the rotation interval. Size-based rotation handles the actual problem (bounded file sizes for shippers) without a scheduler.

8. **Disk-full behavior: log one orchestrator-level error, then stop disk writing globally.** When a write fails with `ENOSPC` (or any persistent OS-level error), the disk writer logs a single structured error to the orchestrator's stdout, marks itself disabled for the lifetime of the orchestrator process, and stops attempting writes for any container. The Docker follow streams and the WebSocket fan-out are unaffected — operators can still live-tail a problem container while the disk is full, which is exactly the moment they need to. Auto-resume on space recovery is intentionally not attempted; recovery is "free up space, restart the orchestrator." This avoids log spam in the edge-case where the disk is right at the boundary, and it keeps the writer's state machine trivial.

9. **The sample compose stack ships with retention enabled.** Most homelab operators expect persistence by default; operators with their own logging pipeline will recognize the env var and unset it. The compose file carries an inline comment about the disk-usage tradeoff and points at `docs/observability.md`.

10. **Disk-backed historical-query REST API is out of scope for v1.** The new disk capture is invisible to REST in v1 and surfaces only via the WebSocket plan and direct disk access. A follow-up pagination plan will broaden `GET /logs` (and standardize pagination across every list endpoint) before either plan is considered released. See Follow-Up Work.

---

## Implementation Notes

Enough to break this into tickets. Not exhaustive.

### New module: `orchestrator/log_capture.py`

A `LogCaptureManager` class, owned by the lifespan in `app.py` alongside `SocketManager` and `ContainerManager`.

Responsibilities:

- For a given Drover container ID and Docker container ID, open and own the follow stream against Docker's logs API.
- Parse the multiplexed Docker stream format (8-byte header `[stream_type][0][0][0][len:4]`, then `len` bytes of payload). Yield `(stream, data, time)` chunks.
- Append each chunk as a JSON line to the current open log file. Rotate when size exceeds the configured threshold.
- On a persistent write error (e.g. `ENOSPC`), log one structured error to orchestrator stdout, set an internal `_disk_disabled = True` flag, and stop attempting writes for the rest of this orchestrator process's lifetime. The follow stream and any non-disk consumers continue to operate.
- Update the in-memory `last_ts` and persist it to `.cursor` (atomic write: temp-file + rename, no fsync per write, fsync per rotation). Skipped when `_disk_disabled` is set.
- Fan out parsed chunks to subscribers — this is where the WebSocket connection manager (from the WebSocket streaming plan) plugs in. The exact interface (queue, callback, observable) is shared with that plan; pick whichever shape that plan settles on.
- Provide `start(container_id, docker_id, since=None)`, `stop(container_id)`, and `discard(container_id)` methods called by `ContainerManager` on lifecycle transitions.

A historical-query method (`read_range(container_id, since, until, limit, offset)`) is **not** part of v1 — it is the responsibility of the follow-up pagination plan. The on-disk format is designed to make it trivial to add later (timestamps are first-class, files are append-only, files are ordered by name).

### Changes to existing modules

- **`orchestrator/config.py`:** add the two new env vars. `DROVER_LOG_DIR` is `Optional[str]`; `DROVER_LOG_MAX_FILE_BYTES` is read but unused when `DROVER_LOG_DIR` is `None`.
- **`orchestrator/docker_client.py`:** add `stream_container_logs(container_id, *, since: float | None, follow: bool, tail: int | None = None) -> AsyncIterator[bytes]` that opens the follow stream and yields raw bytes. The multiplex parsing belongs in `log_capture.py`, not here.
- **`orchestrator/container_manager.py`:** call into `LogCaptureManager` from the lifecycle methods listed in the table above. The critical placement is inside `_init_container`: call `LogCaptureManager.start(container_id, docker_id)` immediately after `await self._docker.start_container(docker_id)` succeeds, *not* in `on_container_ready`. This is what gives us init-window log capture. On resume, pass the cursor; on stop, signal the writer to flush; on destroy and on `_fail_init`, signal the writer to close. The lifecycle calls happen unconditionally; the manager itself decides whether a disk writer is attached based on configuration.
- **`orchestrator/routers/containers.py`:** **no changes in v1.** The existing `GET /containers/{id}/logs` continues to proxy Docker. The pagination follow-up plan owns the broadening.
- **`orchestrator/app.py`:** instantiate `LogCaptureManager` in `lifespan` (with or without disk writer based on config), call its shutdown method on app exit (closes all active capture streams; flushes files when applicable).
- **`docker-compose.yml`:** set `DROVER_LOG_DIR=/var/lib/orchestrator/logs` (sharing the existing `drover-data` volume) with an inline comment explaining the disk-usage tradeoff and pointing at `docs/observability.md`.
- **`README.md`:** document the new env vars and link to `docs/observability.md` for the full retention story (modes, on-disk format, log-shipper examples). The README itself stays a quickstart; deep dives live in observability.md.
- **`docs/observability.md` (new):** the operator-facing reference for everything log-related. See the Documentation Impact section for an outline.
- **`docs/planning/websocket-streaming-plan.md`:** update Phase 2 and Open Question 3 to reference this plan; the `/ws/.../logs` endpoint reads from the shared `LogCaptureManager` rather than opening its own Docker stream. **Also update the connect-time check in that plan**: today it specifies "container not running → close with 1008." That check needs to allow `initializing` so a debugger can attach to a stuck startup; it should reject only `error`, `destroying`, and `destroyed`.
- **`TODO.md`:** remove the "Container log retention" section once this is implemented; add an entry pointing at the follow-up pagination plan as the prerequisite for cutting a release that includes either log retention or WebSocket streaming.

### Tests

- Unit test the multiplex parser against synthetic Docker streams (interleaved stdout/stderr, partial frames spanning chunk boundaries, zero-length frames).
- Unit test rotation: write past threshold, assert new file opens at next write.
- Unit test cursor: write some chunks, simulate restart, assert resume uses the recorded `since`.
- Unit test disk-full: arrange a writer to receive `ENOSPC` on its next write, assert one error log is emitted, assert subsequent writes are no-ops, assert the follow stream and any non-disk consumers continue to receive chunks.
- Integration test against the orchestrator stack (similar to existing `tests/`): create container, run a chatty workload, stop, resume, read the captured files off disk, destroy, verify directory is gone.
- Integration test for init-window capture: launch a micro-container whose entrypoint emits stdout *before* the guest agent connects (e.g. an image that sleeps 2s, prints a message, then execs the agent), then assert the message is present in the captured logs even though the container reached `running` only after the print. Run a parallel test where the entrypoint never starts the agent and the container fails with `init_timeout`, then assert the entrypoint's pre-failure stdout was captured.

### Sequencing

1. Land `LogCaptureManager` (capture, rotation, on-disk format, disk-full handling, no API surface yet) and unit tests.
2. Wire into `ContainerManager` lifecycle. Now logs land on disk and are deleted on destroy. WebSocket and REST endpoints are unchanged.
3. Plug into the WebSocket fan-out as part of the WebSocket plan Phase 2.
4. Documentation pass: `docs/observability.md`, `README.md` link, `docker-compose.yml` env-var addition, ADR for the on-disk-format decision.

Steps 1–2 are independently shippable and useful (operators get on-disk retention they can read directly). Step 3 depends on the WebSocket plan landing or moving in parallel. The follow-up pagination plan is prerequisite to a release that *advertises* log retention as a feature, since users will reasonably expect API access to retained logs.

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Disk usage grows unboundedly for long-running noisy containers. | Rotation by size keeps individual files bounded for shippers. Operator monitoring of `DROVER_LOG_DIR` is the safety net. Document expected disk usage in `docs/observability.md`. |
| Disk fills up. | Single orchestrator-level error log on first failed write, then disk writes are disabled for the rest of the orchestrator process lifetime. The follow stream and WebSocket fan-out continue to operate so the operator can still live-tail the problem container. Recovery is "free up space, restart orchestrator." |
| Orchestrator restart causes gap or duplication in captured logs. | `.cursor` file with `since=` resume. Documented behavior under crash recovery: prefer dupes to gaps. |
| Docker daemon's own log driver buffer is smaller than our capture rate, so logs are lost before we read them. | We open a follow stream the moment Docker `start_container` returns; Docker streams in real time as long as the connection is held. The capture task is high-priority and not gated on anything else. If the asyncio event loop is blocked, both Drover and the WebSocket fan-out are equally affected — this is an orchestrator-health concern, not a logging concern. |
| Filesystem semantics on the host (e.g. SMB-mounted log dir) cause partial writes. | Append-only writes with newline terminator; partial writes look like a truncated last line, which all log shippers handle. Recommend in `docs/observability.md` that `DROVER_LOG_DIR` lives on local storage. |
| Operator's existing log shipper picks up our files *and* Docker's `json-file` files for the same container, producing duplicates downstream. | Documented in `docs/observability.md` with a "pick one" guide. The shipper's own dedupe (or path filtering) is the right place to handle it; the two paths exist for different reasons — Docker's path is the operator's stack, our path is Drover's retention. |
| Permissions: orchestrator runs as UID 1000; if the log volume is owned differently the capture fails on first write. | The orchestrator already creates `/var/lib/orchestrator` with the right ownership; logs live inside that directory in the sample stack so the same Dockerfile step covers them. Document the ownership requirement for operator-supplied volumes mounted at non-default paths. |

Rollback: this work is additive and isolatable. If something goes wrong in production, the operator can simply unset `DROVER_LOG_DIR` to drop into capture-only mode, where Drover writes nothing to disk and behavior matches today's, modulo the live WebSocket tailing path. Reverting the code is also low-risk because no existing data structures change.

---

## Documentation Impact

Files that need updates when this lands:

- **`docs/observability.md` (new):** the operator-facing reference for everything log-related. Outline:
    1. *The three streams Drover emits.* Orchestrator structured logs, micro-container stdout/stderr (the captured stream), per-command stdout/stderr. Where each lives, how to access each.
    2. *Modes.* Drover-managed retention vs capture-only, when to pick each.
    3. *On-disk format and directory layout.* Exact line format, file naming, rotation behavior.
    4. *Shipping logs to external systems.* Promtail/Loki snippet, Vector snippet, Fluent Bit pointer. A note that Drover does not configure Docker log drivers — whatever the operator sets at the daemon level applies.
    5. *Disk usage and the "two copies" tradeoff.* When `DROVER_LOG_DIR` is set, Drover and Docker's log driver each keep a copy. Guidance on which to disable if disk is tight.
    6. *Disk-full behavior.* What happens, how to recover.
    7. *Lifecycle and retention guarantees.* Logs persist until the container is destroyed; what destroying does; what stopping does; what `error` does.
- **`README.md`:** new env-var rows in the configuration table; one-paragraph summary of the retention model with a link to `docs/observability.md` for the deep dive. The README stays a quickstart.
- **`docker-compose.yml`:** add `DROVER_LOG_DIR=/var/lib/orchestrator/logs` to the orchestrator service env vars with an inline comment about disk usage and a pointer to `docs/observability.md`.
- **`TODO.md`:** remove the "Container log retention" section; add an entry referencing the follow-up pagination plan as a release prerequisite.
- **`docs/planning/websocket-streaming-plan.md`:** update Phase 2 to describe the consumption from `LogCaptureManager` rather than opening its own Docker stream, update Open Question 3 to point to this plan, and update the connect-time status check to allow `initializing`.
- **`docs/planning/<pagination-plan>.md` (new, separate effort):** see Follow-Up Work.
- **`docs/decisions/`:** new ADR capturing the on-disk-format decision (Docker `json-file` line format) since that is the kind of decision a future engineer might reasonably want to revisit and needs to know the reasoning for.

Possibly worth a separate ADR on retention semantics ("logs live until the container is destroyed") if it turns out to be load-bearing for later product decisions; not strictly required for the v1 implementation.

---

## Follow-Up Work

Two pieces of work are deliberately deferred out of this plan and into a separate effort, but are prerequisites to a release that includes log retention or WebSocket streaming as advertised features.

### Pagination plan (separate document)

This plan, the WebSocket streaming plan, and any future "list things" endpoint share an unsolved problem: there is no consistent pagination contract. Adding pagination to `GET /containers/{id}/logs` in isolation would set a precedent that the rest of the API would then have to either follow or contradict. Better to design the contract once and apply it everywhere.

**Intent for the pagination plan:**

A consistent four-parameter contract across every list-style REST endpoint (containers, images, command messages, retained logs, anything we add later):

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `since` | RFC3339 timestamp | start of available data | Inclusive lower bound. Applies to time-keyed resources (logs, command messages); for non-time-keyed lists, treated as a no-op or rejected per endpoint. |
| `until` | RFC3339 timestamp | now (or end of available data) | Inclusive upper bound. Optional. |
| `limit` | int | endpoint-specific sensible default (e.g. 100 for command messages, 1000 for logs) | Hard cap per endpoint, also documented. |
| `offset` | int | 0 | For when time bounds and limit aren't sufficient (cursoring within a single timestamp's worth of records). |

Response includes a `next_offset` (or similar) hint if more data is available beyond the limit, plus the effective `since`/`until`/`limit` echoed back so the client can chain calls.

**Endpoints the pagination plan should retrofit, at minimum:**

- `GET /containers` (currently returns everything in one shot)
- `GET /containers/{id}/exec/{command_id}` messages array
- `GET /containers/{id}/logs` — broadened to read from disk when `DROVER_LOG_DIR` is set, falling back to Docker for live tails of running containers
- `GET /images` (currently unbounded)

**Release coupling:**

We will not cut a release that publicly advertises log retention or WebSocket streaming until both this plan and the pagination plan are landed. Operators expecting "I can see my container's old logs" should not need to teach themselves the on-disk format to do it. Until both ship, the new capabilities live on `main` but are documented as preview.

### Documentation handoff

Once the pagination plan exists and is approved, this plan's "Open Questions" stay closed but the API surface section here gets a one-line update pointing at the broadened endpoint. No re-litigation of decisions made here.
