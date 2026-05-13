# WORKING — Container Log Retention

Concrete, sequenced checklist for implementing `docs/planning/container-log-retention.md`. Items are grouped by the three sequencing milestones in the plan. Each box is meant to be small enough that a single PR could land it.

Two clarifications resolved up front (deviations from / additions to the plan):

- **`GET /containers/{id}/logs` does not exist today.** The plan describes it as "the existing endpoint." We will add it now as a thin proxy over `DockerClient.get_container_logs()` so the plan's language matches reality. The pagination follow-up still owns broadening it.
- **One JSON object per multiplex chunk, not per output line.** Docker's `json-file` driver splits at newlines, but we will not — we emit one `{"log","stream","time"}` per parsed frame. This is a deliberate simplification of the "verbatim json-file" claim; document it in `docs/observability.md`.

---

## Milestone 1 — `LogCaptureManager` in isolation

Land the new module with its tests. No wiring into the lifecycle yet, no API surface, no compose changes. Reviewable on its own.

### 1.1 Config

- [ ] `orchestrator/config.py`: add `log_dir: str | None` (env `DROVER_LOG_DIR`, default `None`).
- [ ] `orchestrator/config.py`: add `log_max_file_bytes: int` (env `DROVER_LOG_MAX_FILE_BYTES`, default `10 * 1024 * 1024`).
- [ ] `tests/test_config.py`: cover both env vars (set / unset / default).

### 1.2 Docker streaming primitive

- [ ] `orchestrator/docker_client.py`: add `stream_container_logs(container_id, *, since: float | int | None, follow: bool = True, tail: int | None = None) -> AsyncIterator[bytes]`. Yields raw bytes from the multiplexed `/containers/{id}/logs?stdout=1&stderr=1&follow=1&timestamps=1` response.
- [ ] Use `httpx.AsyncClient.stream()` so the body is consumed incrementally; pass `timeout=None` (or a long read timeout) so a quiet container doesn't trip the default 30s.
- [ ] On `404` / `409`, raise `ContainerNotFoundError` / `ContainerConflictError` the same way the existing helpers do.
- [ ] Unit test the new client method against a fake transport that returns a canned multiplex byte stream.

### 1.3 New module: `orchestrator/log_capture.py`

- [ ] Multiplex parser: consume the byte stream, parse 8-byte headers (`[stream_type, 0, 0, 0, len:u32be]`), assemble payloads across chunk boundaries, yield `(stream: "stdout"|"stderr", payload: bytes, ts: str)`. The `ts` is parsed from the leading `RFC3339Nano ` prefix Docker prepends when `timestamps=1` is set; the rest is the payload bytes.
- [ ] `LogCaptureManager` class with internal state: `{container_id: _Capture}`, a shared `_disk_disabled: bool`, a reference to the config, and a reference to `DockerClient`.
- [ ] `start(container_id, docker_id, since: str | None = None)`:
    - No-op when `config.log_dir is None`.
    - Create `{log_dir}/{container_id}/` (parents OK, mode 0o755).
    - Pick the next file: highest existing `N.log` or `0.log` if none; if that file already exceeds `log_max_file_bytes`, open `{N+1}.log`.
    - Launch a background `asyncio.Task` that consumes `docker.stream_container_logs(...)` and writes parsed chunks. Track the task and the writer state in `_Capture`.
- [ ] `stop(container_id)`: signal the writer to flush, close the file, persist `.cursor`, and exit cleanly. Await the task. Idempotent.
- [ ] `discard(container_id)`: stop the writer if running, then `shutil.rmtree({log_dir}/{container_id}, ignore_errors=True)`. Idempotent. No-op when `log_dir is None`.
- [ ] `shutdown()`: stop all active captures concurrently. Called from the FastAPI `lifespan`.
- [ ] Per-chunk write: serialize `{"log": payload.decode("utf-8", "replace"), "stream": stream, "time": ts}` with `json.dumps` (ensure_ascii=False) and append `\n`. One JSON object per chunk — do not split on newlines (see header note above).
- [ ] Rotation: before each write, if the current file's size + the line length > `log_max_file_bytes`, close it, increment N, open `{N+1}.log`.
- [ ] Cursor: hold `last_ts` in memory; persist it to `{dir}/.cursor` via temp-file-then-rename on rotation and on `stop()`. Use `fsync(file)` on rotation only.
- [ ] Disk-full handling: catch `OSError` (specifically check `errno.ENOSPC`, but treat any persistent write failure the same). Set `_disk_disabled = True` on the manager (shared, not per-container), log one structured error to `logger.error(...)` with the container id and errno, and have all subsequent writes (every container) short-circuit to a no-op. Do not re-attempt and do not re-enable.
- [ ] Cooperative cancellation: the writer task must handle `asyncio.CancelledError` cleanly — flush the current file, persist `.cursor`, then re-raise.

### 1.4 Tests for `log_capture.py` (`tests/test_log_capture.py`)

- [ ] Multiplex parser: synthetic streams covering (a) interleaved stdout/stderr, (b) a single logical frame split across two byte chunks, (c) zero-length payload, (d) malformed/truncated header at end-of-stream (graceful exit).
- [ ] Timestamp parsing: payload with the `RFC3339Nano ` prefix is split correctly; payload without one falls back to a server-side `datetime.now(timezone.utc).isoformat()` (decide and document).
- [ ] Rotation: write enough bytes to cross `log_max_file_bytes`, assert `0.log` is closed and `1.log` opens at the next write; assert size of `0.log` is ≤ threshold.
- [ ] Cursor resume: write some chunks, call `stop()`, assert `.cursor` exists and contains the last `time`; reopen with that `since` and assert it's passed through to the Docker stream call.
- [ ] Disk-full: monkeypatch the file write to raise `OSError(ENOSPC)`; assert exactly one `ERROR`-level log record is emitted, that `_disk_disabled` flips to `True`, and that a subsequent `write` on a *different* container is also a no-op.
- [ ] `discard()`: assert directory removal; assert idempotency when called twice; assert no-op when `log_dir is None`.

---

## Milestone 2 — Lifecycle wiring

After this milestone, captured logs land on disk for every container's full lifetime, removed only on destroy. No new REST surface yet beyond the small live-tail proxy in 2.4.

### 2.1 Plumbing

- [ ] `orchestrator/app.py`: instantiate `LogCaptureManager(config, docker)` in `lifespan`. Store it on `app.state.log_capture`. Pass it into `ContainerManager`.
- [ ] `orchestrator/app.py`: call `await app.state.log_capture.shutdown()` during teardown (before `docker.close()`).
- [ ] `orchestrator/container_manager.py`: accept `LogCaptureManager` in `__init__`; store as `self._logs`.

### 2.2 Lifecycle hooks (the table in the plan, line-by-line)

- [ ] `_init_container`: immediately after `await self._docker.start_container(docker_id)` succeeds (and before the function returns), call `await self._logs.start(container_id, docker_id, since=None)`. This is the init-window-capture requirement — do not move it into `on_container_ready`.
- [ ] `_fail_init`: after the conditional UPDATE succeeds and *before* `remove_container`, call `await self._logs.stop(container_id)`. Do NOT call `discard` — directories on errored containers are preserved for post-mortem.
- [ ] `stop_container`: after `await self._docker.stop_container(...)` returns (or 404s), call `await self._logs.stop(container_id)`. The follow stream will already be closing; this just awaits the writer task and persists the final cursor.
- [ ] `resume_container`: before `await self._docker.start_container(...)`, read the `.cursor` for this container (helper on `LogCaptureManager`, returns `str | None`). After start succeeds, call `await self._logs.start(container_id, row["docker_id"], since=cursor_or_none)`.
- [ ] `destroy_container`: after `remove_container` returns (or 404s) and before the final status UPDATE, call `await self._logs.discard(container_id)`. This applies to destroying `error` and `initializing` rows too — the plan is explicit that partial init logs go with the rest of the state.
- [ ] `sync_containers`:
    - For rows mapped to `running` after reconciliation: call `await self._logs.start(container_id, docker_id, since=cursor_or_None)`.
    - For rows in `stopped`: no action (writer is not running, directory is preserved).
    - For rows mapped to `destroyed` by reconciliation: call `await self._logs.discard(container_id)` to be safe.
    - For the post-reconciliation `_fail_init` of stuck-`initializing` rows: existing `_fail_init` call already triggers `stop`; verify (don't double-call).

### 2.3 Tests

- [ ] Extend `tests/test_container_manager.py` with a fake `LogCaptureManager` (or use the real one against a temp directory) and assert:
    - `start` is invoked exactly once per init success, with `since=None`.
    - `start` is *not* invoked when Docker `start_container` raises.
    - `stop` is invoked once on `stop_container`, once on `_fail_init`.
    - `start` is invoked again on `resume_container` with the cursor value when one exists, `None` when it doesn't.
    - `discard` is invoked exactly once on `destroy_container`, including the `error` and `initializing` cases.
    - `sync_containers` re-issues `start` for rows that come back as `running`.
- [ ] New integration-style test (or extension of the existing harness if there is one): create container, exec a chatty workload, stop, resume, read files off disk, assert content is present and ordered, destroy, assert directory is gone.
- [ ] Init-window test: image whose entrypoint prints a sentinel string and sleeps 2s before exec'ing the agent. Capture and assert the sentinel is in `0.log`.
- [ ] Init-timeout test: image that never starts the agent. After `init_timeout`, assert the row is `error`, the entrypoint's pre-failure stdout is in `0.log`, and the directory is still there. Destroy and assert it's gone.

### 2.4 Add the missing `/logs` REST endpoint (deviation from plan)

- [ ] `orchestrator/routers/containers.py`: add `GET /containers/{container_id}/logs` that calls `DockerClient.get_container_logs()` and returns it as `text/plain`. Accept an optional `?tail=N` query parameter (default `"all"`). Return 404 if the container row is missing; let `DockerError`/`ContainerNotFoundError` propagate to the existing handlers.
- [ ] Document in the docstring and in `README.md` that this is a thin live proxy of Docker's logs API — no on-disk history yet, that's the pagination follow-up.
- [ ] Unit test: 200 for a running container, 404 for an unknown container, 502 when Docker errors.

---

## Milestone 3 — Documentation, sample stack, and release prerequisites

After this milestone, an operator can use the feature without reading the code.

### 3.1 Sample compose

- [ ] `docker-compose.yml`: under `orchestrator.environment`, add `DROVER_LOG_DIR: /var/lib/orchestrator/logs` with an inline comment about (a) the doubled-disk-usage tradeoff vs. Docker's own log driver and (b) `docs/observability.md` for the full story. Logs live inside the existing `drover-data` volume — no new volume needed.
- [ ] Manual smoke test: bring up the stack, create + exec + destroy a container, confirm `{drover-data}/logs/{id}/...` appears and goes away on destroy.

### 3.2 New: `docs/observability.md`

Outline from the plan, expanded:

- [ ] Section 1 — *The three streams Drover emits.* Orchestrator structured JSON logs (Docker daemon's own driver), micro-container stdout/stderr (this feature), per-command stdout/stderr (SQLite, exposed via `/exec/{cmd_id}`).
- [ ] Section 2 — *Modes.* `DROVER_LOG_DIR` set vs unset; recommend set for homelab, unset for operators with Loki/journald already.
- [ ] Section 3 — *On-disk format and directory layout.* The exact line format. Call out the one-JSON-per-chunk decision so anyone wiring Promtail's `docker` pipeline stage knows what to expect (a `log` field may contain multiple newlines).
- [ ] Section 4 — *Shipping logs to external systems.* Promtail snippet pointing at `DROVER_LOG_DIR`; pointer to Vector's `file` source and Fluent Bit's `tail` input. Note that the existing Docker daemon log driver is independent of this.
- [ ] Section 5 — *Disk-usage and the "two copies" tradeoff.* How to disable one or the other.
- [ ] Section 6 — *Disk-full behavior.* What the operator sees (one structured error in orchestrator stdout), the recovery procedure (free space, restart orchestrator).
- [ ] Section 7 — *Lifecycle and retention guarantees.* Logs persist until destroy; `error` rows keep their logs until destroyed; orchestrator-restart resume semantics; possible duplicates on crash recovery.
- [ ] Section 8 — *Live tail.* Note that `GET /containers/{id}/logs` is a thin Docker proxy and does not (yet) read from the on-disk history; pagination follow-up will fix that.

### 3.3 README

- [ ] `README.md`: add `DROVER_LOG_DIR` and `DROVER_LOG_MAX_FILE_BYTES` rows to the configuration table.
- [ ] `README.md`: one-paragraph summary of the retention model, linking to `docs/observability.md`.

### 3.4 ADRs

- [ ] `docs/decisions/<YYYY-MM-DD>-on-disk-log-format.md`: capture the decision to use Docker's `json-file` line format and the one-JSON-per-chunk deviation. Future engineer needs to know why we didn't split on newlines.
- [ ] (Optional) `docs/decisions/<YYYY-MM-DD>-retention-on-destroy.md` if we decide it's load-bearing for later product decisions. Defer unless asked.

### 3.5 TODO.md

- [ ] Remove the "Container log retention" section.
- [ ] Add a one-line entry pointing at the (future, separate) pagination plan as the prerequisite for cutting a release that advertises this feature.

---

## Out of scope (do not do in this branch)

These are deliberately deferred per the plan; mention only to keep them off the checklist:

- A `read_range(container_id, since, until, limit, offset)` method on `LogCaptureManager`.
- Broadening `GET /containers/{id}/logs` to read from disk.
- Pagination on any list endpoint.
- Time-based retention or compression.
- A bundled Loki/Promtail/Grafana profile in `docker-compose.yml`.

---

## Open questions to surface during review

- Should the writer record a fallback `time` when Docker's `timestamps=1` prefix is missing or malformed? Suggested default: use `datetime.now(timezone.utc).isoformat(timespec="microseconds") + "Z"` and log a `DEBUG` line. Confirm during 1.3.
- What `since` resolution does Docker accept on the logs API in practice (integer seconds vs. RFC3339Nano)? Verify against the daemon during 1.4 cursor tests — if it only accepts seconds, the cursor file should store seconds and we accept up to one second of duplicates on resume.
- Permissions: the orchestrator runs as UID 1000 and the existing Dockerfile chowns `/var/lib/orchestrator`. Confirm `mkdir({log_dir}/{id})` succeeds in the sample stack without a new entrypoint step.
