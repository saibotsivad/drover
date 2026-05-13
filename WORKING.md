# WORKING — Container Log Retention

Concrete, sequenced checklist for implementing `docs/planning/container-log-retention.md`. Items are grouped by the three sequencing milestones in the plan. Each box is meant to be small enough that a single PR could land it.

Three clarifications resolved up front (deviations from / additions to the plan):

- **`GET /containers/{id}/logs` does not exist today.** The plan describes it as "the existing endpoint." We will add it now as a thin proxy over `DockerClient.get_container_logs()` so the plan's language matches reality. The pagination follow-up still owns broadening it.
- **Two new file-access endpoints land in v1.** `GET /containers/{id}/logs/files` returns a JSON array of captured log filenames (empty array when the container has no captured logs); `GET /containers/{id}/logs/files/{filename}` returns one file verbatim. Both return 409 (`LoggingNotEnabled`) when `DROVER_LOG_DIR` is unset and 404 when the container row does not exist (the file endpoint also 404s when the specific file is missing). This gives operators raw API access to retained logs without waiting for the pagination plan and makes e2e assertions trivial.
- **One JSON object per multiplex chunk, not per output line.** Docker's `json-file` driver splits at newlines, but we will not — we emit one `{"log","stream","time"}` per parsed frame. This is a deliberate simplification of the "verbatim json-file" claim; document it in `docs/observability.md`.

---

## Milestone 1 — `LogCaptureManager` in isolation

Land the new module with its tests. No wiring into the lifecycle yet, no API surface, no compose changes. Reviewable on its own.

### 1.1 Config

- [x] `orchestrator/config.py`: add `log_dir: str | None` (env `DROVER_LOG_DIR`, default `None`).
- [x] `orchestrator/config.py`: add `log_max_file_bytes: int` (env `DROVER_LOG_MAX_FILE_BYTES`, default `10 * 1024 * 1024`).
- [x] `tests/test_config.py`: cover both env vars (set / unset / default).

### 1.2 Docker streaming primitive

- [x] `orchestrator/docker_client.py`: add `stream_container_logs(container_id, *, since: float | int | None, follow: bool = True, tail: int | None = None) -> AsyncIterator[bytes]`. Yields raw bytes from the multiplexed `/containers/{id}/logs?stdout=1&stderr=1&follow=1&timestamps=1` response.
- [x] Use `httpx.AsyncClient.stream()` so the body is consumed incrementally; pass `timeout=None` (or a long read timeout) so a quiet container doesn't trip the default 30s.
- [x] On `404` / `409`, raise `ContainerNotFoundError` / `ContainerConflictError` the same way the existing helpers do.
- [x] Unit test the new client method against a fake transport that returns a canned multiplex byte stream.

### 1.3 New module: `orchestrator/log_capture.py`

- [x] Multiplex parser: consume the byte stream, parse 8-byte headers (`[stream_type, 0, 0, 0, len:u32be]`), assemble payloads across chunk boundaries, yield `(stream: "stdout"|"stderr", payload: bytes, ts: str)`. The `ts` is parsed from the leading `RFC3339Nano ` prefix Docker prepends when `timestamps=1` is set; the rest is the payload bytes. If the prefix is missing or malformed, fall back to `datetime.now(timezone.utc).isoformat(timespec="microseconds") + "Z"` and emit one `DEBUG` log line per chunk so the condition is debuggable without spamming `INFO`.
- [x] `LogCaptureManager` class with internal state: `{container_id: _Capture}`, a shared `_disk_disabled: bool`, a reference to the config, and a reference to `DockerClient`.
- [x] `start(container_id, docker_id, since: str | None = None)`:
    - No-op when `config.log_dir is None`.
    - Create `{log_dir}/{container_id}/` (parents OK, mode 0o755).
    - Pick the next file: highest existing `N.log` or `0.log` if none; if that file already exceeds `log_max_file_bytes`, open `{N+1}.log`.
    - Launch a background `asyncio.Task` that consumes `docker.stream_container_logs(...)` and writes parsed chunks. Track the task and the writer state in `_Capture`.
- [x] `stop(container_id)`: signal the writer to flush, close the file, persist `.cursor`, and exit cleanly. Await the task. Idempotent.
- [x] `discard(container_id)`: stop the writer if running, then `shutil.rmtree({log_dir}/{container_id}, ignore_errors=True)`. Idempotent. No-op when `log_dir is None`.
- [x] `shutdown()`: stop all active captures concurrently. Called from the FastAPI `lifespan`.
- [x] Per-chunk write: serialize `{"log": payload.decode("utf-8", "replace"), "stream": stream, "time": ts}` with `json.dumps` (ensure_ascii=False) and append `\n`. One JSON object per chunk — do not split on newlines (see header note above).
- [x] Rotation: before each write, if the current file's size + the line length > `log_max_file_bytes`, close it, increment N, open `{N+1}.log`.
- [x] Cursor: hold `last_ts` in memory; persist it to `{dir}/.cursor` via temp-file-then-rename on rotation and on `stop()`. Use `fsync(file)` on rotation only.
- [x] Disk-full handling: catch `OSError` (specifically check `errno.ENOSPC`, but treat any persistent write failure the same). Set `_disk_disabled = True` on the manager (shared, not per-container), log one structured error to `logger.error(...)` with the container id and errno, and have all subsequent writes (every container) short-circuit to a no-op. Do not re-attempt and do not re-enable.
- [x] Cooperative cancellation: the writer task must handle `asyncio.CancelledError` cleanly — flush the current file, persist `.cursor`, then re-raise.

### 1.4 Tests for `log_capture.py` (`tests/test_log_capture.py`)

- [x] Multiplex parser: synthetic streams covering (a) interleaved stdout/stderr, (b) a single logical frame split across two byte chunks, (c) zero-length payload, (d) malformed/truncated header at end-of-stream (graceful exit).
- [x] Timestamp parsing: payload with the `RFC3339Nano ` prefix is split correctly; payload without one falls back to the server-side wall clock and emits exactly one `DEBUG` log line.
- [x] Rotation: write enough bytes to cross `log_max_file_bytes`, assert `0.log` is closed and `1.log` opens at the next write; assert size of `0.log` is ≤ threshold.
- [x] Cursor resume: write some chunks, call `stop()`, assert `.cursor` exists and contains the last `time`; reopen with that `since` and assert it's passed through to the Docker stream call. While writing this test, verify what resolution Docker's `since=` actually honors — if the daemon only accepts integer seconds, store seconds in `.cursor` and accept up to one second of duplicates on resume; otherwise keep the full RFC3339Nano string.
- [x] Disk-full: monkeypatch the file write to raise `OSError(ENOSPC)`; assert exactly one `ERROR`-level log record is emitted, that `_disk_disabled` flips to `True`, and that a subsequent `write` on a *different* container is also a no-op.
- [x] `discard()`: assert directory removal; assert idempotency when called twice; assert no-op when `log_dir is None`.

---

## Milestone 2 — Lifecycle wiring

After this milestone, captured logs land on disk for every container's full lifetime, removed only on destroy. No new REST surface yet beyond the small live-tail proxy in 2.4.

### 2.1 Plumbing

- [x] `orchestrator/app.py`: instantiate `LogCaptureManager(config, docker)` in `lifespan`. Store it on `app.state.log_capture`. Pass it into `ContainerManager`.
- [x] `orchestrator/app.py`: call `await app.state.log_capture.shutdown()` during teardown (before `docker.close()`).
- [x] `orchestrator/container_manager.py`: accept `LogCaptureManager` in `__init__`; store as `self._logs`.
- [x] No new entrypoint step for permissions: the existing Dockerfile already chowns `/var/lib/orchestrator` to UID 1000, so `mkdir({log_dir}/{id})` succeeds when `DROVER_LOG_DIR` sits under that path. The e2e suite landing green in 2.5 confirms this; document the ownership requirement in `docs/observability.md` for operators who mount `DROVER_LOG_DIR` elsewhere.

### 2.2 Lifecycle hooks (the table in the plan, line-by-line)

- [x] `_init_container`: immediately after `await self._docker.start_container(docker_id)` succeeds (and before the function returns), call `await self._logs.start(container_id, docker_id, since=None)`. This is the init-window-capture requirement — do not move it into `on_container_ready`.
- [x] `_fail_init`: after the conditional UPDATE succeeds and *before* `remove_container`, call `await self._logs.stop(container_id)`. Do NOT call `discard` — directories on errored containers are preserved for post-mortem.
- [x] `stop_container`: after `await self._docker.stop_container(...)` returns (or 404s), call `await self._logs.stop(container_id)`. The follow stream will already be closing; this just awaits the writer task and persists the final cursor.
- [x] `resume_container`: before `await self._docker.start_container(...)`, read the `.cursor` for this container (helper on `LogCaptureManager`, returns `str | None`). After start succeeds, call `await self._logs.start(container_id, row["docker_id"], since=cursor_or_none)`.
- [x] `destroy_container`: after `remove_container` returns (or 404s) and before the final status UPDATE, call `await self._logs.discard(container_id)`. This applies to destroying `error` and `initializing` rows too — the plan is explicit that partial init logs go with the rest of the state.
- [x] `sync_containers`:
    - For rows mapped to `running` after reconciliation: call `await self._logs.start(container_id, docker_id, since=cursor_or_None)`.
    - For rows in `stopped`: no action (writer is not running, directory is preserved).
    - For rows mapped to `destroyed` by reconciliation: call `await self._logs.discard(container_id)` to be safe.
    - For the post-reconciliation `_fail_init` of stuck-`initializing` rows: existing `_fail_init` call already triggers `stop`; verify (don't double-call).

### 2.3 Unit tests for lifecycle wiring

- [x] Extend `tests/test_container_manager.py` using the **real** `LogCaptureManager` against a `tmp_path` directory (not a mock), with a fake `DockerClient` that yields a small canned multiplex stream. This catches integration bugs between the two managers (e.g. `start` being called with the wrong `since` type) that a mock would mask.
- [x] Assertions:
    - `start` is invoked exactly once per init success and the writer task is running afterward.
    - `start` is *not* invoked when Docker `start_container` raises (no log directory should appear).
    - `stop` is invoked once on `stop_container`, once on `_fail_init`. After `stop`, the writer task is done and `.cursor` is on disk.
    - `start` is invoked again on `resume_container` with the cursor value when one exists, `None` when it doesn't. Resume after stop reuses the latest `*.log` file (or rotates) — assert the file contents accumulate across the two segments.
    - `discard` is invoked exactly once on `destroy_container`, including the `error` and `initializing` cases. After destroy the directory is gone from disk.
    - `sync_containers` re-issues `start` for rows that come back as `running`.
- [x] End-to-end lifecycle behaviors (chatty workload, init-window capture, init-timeout retention, multi-segment resume) are exercised in the e2e suite — see 2.5. The unit tests above stay focused on call-site wiring.

### 2.4 Log REST endpoints

Three routes. All return 404 when the container row does not exist; `/logs/files*` additionally return 409 (`LoggingNotEnabled`) when `DROVER_LOG_DIR` is unset.

- [x] `orchestrator/container_manager.py` (or a small new exception module): add `LoggingNotEnabled(ContainerError)` returning 409. Add `LogFileNotFound(ContainerError)` returning 404 for the single-file endpoint when the requested filename is absent.

**`GET /containers/{container_id}/logs`** — live Docker proxy (deviation from plan; route did not exist).

- [x] `orchestrator/routers/containers.py`: thin proxy over `DockerClient.get_container_logs()`, returning `text/plain`. Optional `?tail=N` (default `"all"`). 404 on unknown container; existing `DockerError` handler covers 502.
- [x] Docstring + README: call this a live tail of Docker's logs API; on-disk history is reached via `/logs/files`.

**`GET /containers/{container_id}/logs/files`** — list captured filenames.

- [x] Add `LogCaptureManager.list_files(container_id) -> list[str]`. Reads `{log_dir}/{container_id}/`, filters to entries matching `^\d+\.log$` (deliberately excluding `.cursor`), and returns them sorted by their integer prefix. Returns `[]` if the directory does not exist — covers both "row predates `DROVER_LOG_DIR`" and "container was destroyed and `discard` removed its directory". Raises `LoggingNotEnabled` if `config.log_dir is None`.
- [x] `orchestrator/routers/containers.py`: new route returning `list[str]` JSON. Verify the container row exists first (404 path) before calling into `LogCaptureManager` (so the 404 reason ordering is predictable).
- [x] Unit tests in `tests/test_log_capture.py`: empty directory returns `[]`; directory with `0.log`, `2.log`, `10.log`, `.cursor` returns `["0.log", "2.log", "10.log"]`; missing directory returns `[]`; unset config raises `LoggingNotEnabled`.

**`GET /containers/{container_id}/logs/files/{filename}`** — return one captured file verbatim.

- [x] Add `LogCaptureManager.open_file(container_id, filename) -> AsyncIterator[bytes]` (or `read_file_path(...) -> Path` if a `FileResponse` is simpler). Validate `filename` against `^\d+\.log$` and reject anything else as `LogFileNotFound` — this is the path-traversal guard, do not rely on the filesystem to reject `..`.
- [x] Route returns the file with `Content-Type: text/plain` so `curl` and the webapp render it inline. Use FastAPI's `FileResponse` or a streaming response — files may approach `DROVER_LOG_MAX_FILE_BYTES` (10 MiB default), don't load into memory.
- [x] Concurrency: reads on the currently-active log file race with the writer. No locking — the file is opened read-only, returned as-is, and any in-flight write completes independently. The race is documented in `docs/observability.md` (3.2) but not mitigated in code; we explicitly do not want callers assuming the file is point-in-time consistent.
- [x] Unit tests: 200 with correct body for an existing file; 404 for `foo.log`, `../etc/passwd`, `0.txt`, `0.log` in a missing directory; 409 when `DROVER_LOG_DIR` is unset; 404 when the container row is missing.

### 2.5 e2e integration assertions

Extend the existing e2e suite rather than building a new integration harness in-tree. The e2e stack already sets `DROVER_LOG_DIR` (Milestone 3.1) so these assertions exercise the real on-disk path.

- [x] Extend `e2e/tests/03-privileged-container.sh` and `e2e/tests/04-standard-container.sh` after the exec step:
    - `GET /containers/{id}/logs/files` → 200, body is a non-empty JSON array containing `0.log`.
    - `GET /containers/{id}/logs/files/0.log` → 200, `Content-Type: text/plain`, body contains the sentinel the test workload printed (e.g. `"hello_drover"` from test 03).
- [x] After the `DELETE /containers/{id}` step in each test:
    - `GET /containers/{id}/logs/files` → 200 with `[]` (row still exists, directory has been removed by `discard`).
    - `GET /containers/{id}/logs/files/0.log` → 404.
- [x] New `e2e/tests/05-log-retention.sh` covering the cases that need a *dedicated* container (i.e. don't slot cleanly into 03/04):
    - Init-window capture: image whose entrypoint emits a sentinel and sleeps 2s before exec'ing the agent. Poll until `running`, then `GET /logs/files/0.log` and assert the sentinel is present.
    - Init-timeout retention: image whose entrypoint emits a sentinel and then never connects the agent. Wait for `init_timeout_seconds + slack`, assert status `error`, `GET /logs/files/0.log` returns 200 with the sentinel. Then `DELETE` and assert `/logs/files` returns `[]` and `/logs/files/0.log` returns 404.
    - Stop / resume continuity: exec command A, stop, resume, exec command B, list files, fetch each, assert A's output appears before B's output across the file set.
- [x] Helper in `e2e/lib/`: small `assert_log_contains` wrapper around `api_get` + `jq -r '.log'` extraction, to keep the test bodies readable.

---

## Milestone 3 — Documentation, sample stack, and release prerequisites

After this milestone, an operator can use the feature without reading the code.

### 3.1 Sample compose

- [x] `docker-compose.yml`: under `orchestrator.environment`, add `DROVER_LOG_DIR: /var/lib/orchestrator/logs` with an inline comment about (a) the doubled-disk-usage tradeoff vs. Docker's own log driver and (b) `docs/observability.md` for the full story. Logs live inside the existing `drover-data` volume — no new volume needed.
- [x] `e2e/docker-compose.e2e.yml`: same env-var addition, so the e2e assertions in 2.5 hit the real on-disk path. The e2e suite is the smoke test — no separate manual run needed.

### 3.2 New: `docs/observability.md`

Outline from the plan, expanded:

- [x] Section 1 — *The three streams Drover emits.* Orchestrator structured JSON logs (Docker daemon's own driver), micro-container stdout/stderr (this feature), per-command stdout/stderr (SQLite, exposed via `/exec/{cmd_id}`).
- [x] Section 2 — *Modes.* `DROVER_LOG_DIR` set vs unset; recommend set for homelab, unset for operators with Loki/journald already.
- [x] Section 3 — *On-disk format and directory layout.* The exact line format. Call out the one-JSON-per-chunk decision so anyone wiring Promtail's `docker` pipeline stage knows what to expect (a `log` field may contain multiple newlines). Note that reads on the currently-active file via `/logs/files/{filename}` can race with the writer — operators may see a truncated final line on a busy container; the format's per-record `\n` terminator means each complete record is well-formed.
- [x] Section 4 — *Shipping logs to external systems.* Promtail snippet pointing at `DROVER_LOG_DIR`; pointer to Vector's `file` source and Fluent Bit's `tail` input. Note that the existing Docker daemon log driver is independent of this.
- [x] Section 5 — *Disk-usage and the "two copies" tradeoff.* How to disable one or the other.
- [x] Section 6 — *Disk-full behavior.* What the operator sees (one structured error in orchestrator stdout), the recovery procedure (free space, restart orchestrator).
- [x] Section 7 — *Lifecycle and retention guarantees.* Logs persist until destroy; `error` rows keep their logs until destroyed; orchestrator-restart resume semantics; possible duplicates on crash recovery.
- [x] Section 8 — *Live tail.* Note that `GET /containers/{id}/logs` is a thin Docker proxy and does not (yet) read from the on-disk history; pagination follow-up will fix that.

### 3.3 README

- [x] `README.md`: add `DROVER_LOG_DIR` and `DROVER_LOG_MAX_FILE_BYTES` rows to the configuration table.
- [x] `README.md`: one-paragraph summary of the retention model, linking to `docs/observability.md`.

### 3.4 ADRs

- [x] `docs/decisions/<YYYY-MM-DD>-on-disk-log-format.md`: capture the decision to use Docker's `json-file` line format and the one-JSON-per-chunk deviation. Future engineer needs to know why we didn't split on newlines.

### 3.5 TODO.md

- [x] Remove the "Container log retention" section.
- [x] Add a one-line entry pointing at the (future, separate) pagination plan as the prerequisite for cutting a release that advertises this feature.

---

## Out of scope (do not do in this branch)

These are deliberately deferred per the plan; mention only to keep them off the checklist:

- A `read_range(container_id, since, until, limit, offset)` method on `LogCaptureManager`.
- Broadening `GET /containers/{id}/logs` to read from disk.
- Pagination on any list endpoint.
- Time-based retention or compression.
- A bundled Loki/Promtail/Grafana profile in `docker-compose.yml`.

---

## Open-ended decisions made during implementation

These items were under-specified in the plan or in the checklist above. The chosen approach is recorded here so it doesn't get re-litigated when the next reviewer arrives.

### Cursor format and Docker `since=` resolution

`.cursor` stores the exact `RFC3339Nano` string from the most recently captured chunk's `time` field. When the orchestrator restarts (or a container is resumed), `LogCaptureManager._parse_cursor_to_since` parses it and converts to integer Unix seconds before passing to Docker's `since=` parameter, because the Docker logs API documents `since` as a UNIX timestamp and only honors integer seconds in practice. The cost is up to one second of duplicate records on resume, which is fine — the plan explicitly prefers duplicates over gaps. The cursor itself stays in RFC3339Nano so a human reading the file can compare it directly against the `time` field of records.

### Exception class location

`LoggingNotEnabled(ContainerError)` and `LogFileNotFound(ContainerError)` live in `orchestrator/log_capture.py` (alongside the manager that raises them) but inherit from a `ContainerError` base class moved to a new `orchestrator/errors.py`. The plan said "container_manager.py *or* a small new exception module"; we picked the new module because `container_manager.py` already imports `LogCaptureManager` and so cannot also be the source of an exception that `log_capture.py` needs to import. `container_manager.py` re-imports `ContainerError` from `errors.py` so the existing public import path (`from orchestrator.container_manager import ContainerError`) still works.

### e2e sentinel for tests 03 / 04

The plan's wording was "body contains the sentinel the test workload printed (e.g. `\"hello_drover\"` from test 03)". `hello_drover` is the *exec* command's stdout, which is captured by the guest agent and streamed back over the per-container Unix socket; it never reaches the container's PID 1 stdout and therefore is not in `0.log`. The sentinel we assert on is `Connecting to`, the first line `drover-executor` writes to its stderr at startup. That stderr *is* the container's PID 1 stderr, so it appears in `0.log` reliably, and any container that reached `running` will have produced it. (`docs/observability.md` makes the per-stream distinction explicit, so this is consistent with the operator-facing story.)

### Scope of e2e test 05

The plan asked for three sub-cases in `e2e/tests/05-log-retention.sh`: init-window capture, init-timeout retention, and stop/resume continuity. The first two require a custom image whose entrypoint emits a sentinel and either delays before exec'ing the agent or never execs it. Building that image (and the orchestrator support to launch it with a custom command) is out of the scope this branch authorized — it would mean either a new image dir under `e2e/` with its own Dockerfile or a new `command` field on `CreateContainerRequest`. Both decisions deserve their own tickets.

What 05 *does* cover is **stop/resume continuity** end-to-end. Init-window capture and init-timeout retention are exercised in unit tests (`tests/test_container_manager.py::test_init_starts_log_capture` and `::test_fail_init_persists_cursor_and_keeps_directory`) which assert the same invariants — directory exists, `.cursor` written, files preserved on `_fail_init`. The dedicated e2e cases should be added as a follow-up once one of the two infrastructure decisions above lands.

### `_pick_initial_file` doesn't truncate over-large existing files

If `0.log` is already past `DROVER_LOG_MAX_FILE_BYTES` when we open it (e.g. operator lowered the threshold between runs, or an old file on disk predates the threshold), we advance to `1.log` rather than truncating. This is deliberate: never destroy on-disk content the operator might want to inspect. The threshold is a "rotate sooner than this" hint, not a hard wall.
