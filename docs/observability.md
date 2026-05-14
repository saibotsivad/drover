# Observability

Operator-facing reference for everything log-related in Drover. Read
this when you want to ship logs to Loki/Vector/Fluent Bit, when you're
choosing between letting Drover retain logs vs. relying on Docker's own
log driver, or when you need to debug a container after it has stopped.

---

## 1. The three streams Drover emits

| Stream | Source | How to access | Retention |
|---|---|---|---|
| Orchestrator structured logs | The orchestrator process itself, written to stdout as one JSON object per line. | Whatever log driver the host Docker daemon is configured with (`json-file`, `journald`, `loki`, etc.) — `docker logs drover-orchestrator` is the simplest interactive view. For a per-container filtered view, `GET /containers/{id}/logs/orchestrator` returns only the orchestrator log lines that mention a specific container ID. | Driver-specific. Drover does not manage this. |
| Micro-container stdout/stderr | Each Drover-managed container's own stdout/stderr. | Live tail: `GET /containers/{id}/logs` (proxies Docker's logs API). On-disk capture: `GET /containers/{id}/logs/files` and `/logs/files/{filename}` when `DROVER_LOG_DIR` is set (see §2). | When `DROVER_LOG_DIR` is set: kept until the container is destroyed. When unset: whatever Docker's log driver provides. |
| Per-command stdout/stderr | The output of an `exec` command run inside a container, captured by the guest agent and streamed back over the per-container Unix socket. | `GET /containers/{id}/exec/{command_id}` returns the `messages` array from the `command_messages` table. | SQLite-backed; lives until the container row is destroyed. |

The rest of this document is about the second stream — the per-container
stdout/stderr — because that's the one this feature changes.

---

## 2. Modes

There are exactly two modes, controlled by a single environment
variable.

**`DROVER_LOG_DIR` set (recommended for homelab).**

Drover opens a follow stream against Docker's logs API for every
running container and writes the captured output to disk under
`{DROVER_LOG_DIR}/{container_id}/`. The on-disk history survives
container stop, orchestrator restart, and orchestrator upgrade. It is
removed only when the container is destroyed (`DELETE /containers/{id}`).

The sample `docker-compose.yml` ships with this mode enabled at
`/var/lib/orchestrator/logs` (inside the existing `drover-data` volume —
no second volume to manage).

**`DROVER_LOG_DIR` unset (recommended if you already ship Docker logs).**

Drover writes nothing to disk. No directory is created, no `.cursor`
file is maintained, no follow stream is opened. The
`/containers/{id}/logs/files*` endpoints return `409 LoggingNotEnabled`.
Historical-log queries fall through to Docker's own log driver: if you
have `--log-driver=loki` (or journald, or fluentd) at the daemon level,
that's where your container logs live.

---

## 3. On-disk format and directory layout

### Directory layout

```
{DROVER_LOG_DIR}/                         # default sample compose: /var/lib/orchestrator/logs
├── cnt_abc123/
│   ├── 0.log
│   ├── 1.log
│   ├── 2.log                             # current writer is always the highest-numbered file
│   └── .cursor                           # last-written timestamp; used to resume after restart
└── cnt_def456/
    ├── 0.log
    └── .cursor
```

- One directory per Drover container ID.
- Files are named `0.log`, `1.log`, … with monotonically-increasing
  integer suffixes. `ls`, `cat *.log`, and any log shipper that reads
  in alphabetical-by-name order will get them in chronological order
  (with one caveat: if a container accumulates more than 10 files, sort
  by integer prefix not lexicographic).
- `.cursor` holds the timestamp of the most recently written record.
  Treat it as opaque; it exists only so the orchestrator can resume the
  Docker follow stream without gaps after a restart.
- Rotation is **size-only**: when the active file would exceed
  `DROVER_LOG_MAX_FILE_BYTES` on the next write, the writer closes it
  and opens the next-numbered file. There is no time-based rotation,
  so a long-quiet container's `0.log` may stay open for days.
- Keep `DROVER_LOG_DIR` on local storage. Network filesystems (SMB,
  NFS) can produce partial writes that look like a truncated last
  line — log shippers handle that gracefully, but on-disk consistency
  is best on a local POSIX filesystem.

### Line format

We adopt **Docker's `json-file` driver line format verbatim**:

```json
{"log":"Cloning into 'repo'...\n","stream":"stdout","time":"2026-05-13T12:34:56.789012345Z"}
```

One JSON object per line, newline-delimited. Promtail's `docker`
pipeline stage, Vector's `docker_logs` source, Fluent Bit's `docker`
parser, and Loki's docker-driver all expect exactly this format.

**One important deviation from `json-file`:** Docker's `json-file`
driver splits its input on newlines, so each `{log, stream, time}`
record holds exactly one line of output. Drover does **not** do that.
We emit one record per parsed Docker frame, which means the `log` field
may contain multiple `\n` characters. If you wire Promtail's `docker`
pipeline against Drover-captured files and expect the `log` field to be
single-line, your downstream parser will need a `multiline` stage. The
ADR in `docs/decisions/` walks through why we made this trade-off.

### Reads on the active file race with the writer

`GET /containers/{id}/logs/files/{filename}` opens the file read-only
and streams its contents back. There is no lock, so a read on the
currently-active log file may race with an in-flight write — operators
on a busy container can see a truncated final line. Each complete
record is `\n`-terminated, so log shippers handle the partial last line
gracefully (it'll either parse on the next iteration after the writer
finishes, or be discarded). The same race applies if you `tail -f` the
file directly.

---

## 4. Shipping logs to external systems

Drover's captured files are valid `json-file` lines, so any tool that
consumes Docker's `json-file` driver consumes Drover too. The operator
points the shipper at `DROVER_LOG_DIR` and lets it pick everything up.

### Promtail (canonical example)

```yaml
scrape_configs:
  - job_name: drover-containers
    static_configs:
      - targets: [localhost]
        labels:
          job: drover
          __path__: /var/lib/orchestrator/logs/*/*.log
    pipeline_stages:
      - docker: {}
      # If you need single-line records downstream, add a multiline
      # stage here that splits the `log` field on `\n`.  Drover emits
      # one JSON record per Docker frame, not one per line.
```

### Vector

Use the `file` source with `read_from = "beginning"` to reuse Drover's
on-disk history when the shipper restarts:

```toml
[sources.drover]
type = "file"
include = ["/var/lib/orchestrator/logs/*/*.log"]
read_from = "beginning"
```

### Fluent Bit

Use the `tail` input with `Parser docker`:

```ini
[INPUT]
    Name        tail
    Path        /var/lib/orchestrator/logs/*/*.log
    Parser      docker
```

### Docker's own log driver

Drover does not configure or override per-container log drivers — it
never touches `HostConfig.LogConfig`. Whatever you set at the daemon
level (`/etc/docker/daemon.json`, the `--log-driver` flag) continues to
apply, and that's a separate copy of every container's stdout. If you
already ship Docker logs to Loki via the `loki` driver, you'll see
each Drover container's output in Loki without doing anything else —
*and* on disk under `DROVER_LOG_DIR`. See §5 for how to disable one.

---

## 5. Disk usage and the "two copies" trade-off

When `DROVER_LOG_DIR` is set, every container's stdout exists in two
places:

1. Whatever Docker's daemon log driver writes (default
   `/var/lib/docker/containers/{docker_id}/{docker_id}-json.log` for
   `json-file`).
2. Drover's capture under `DROVER_LOG_DIR/{container_id}/`.

The two paths exist for different reasons — Docker's path is for the
operator's own logging stack, Drover's path is for Drover's retention
guarantee — so by default both are kept. To avoid the duplication:

- **Disable Drover's capture** for operators who already have a log
  pipeline: unset `DROVER_LOG_DIR`. Live tails still work via
  `GET /containers/{id}/logs` (proxying Docker's logs API).
- **Disable Docker's per-container copy** by setting
  `--log-driver=none` at the daemon level (or per-service in compose).
  Drover keeps capturing because we use the Docker logs API stream, not
  the disk file. Live tails via `GET /containers/{id}/logs` will return
  empty, but on-disk history at `/logs/files` is intact.

There is no built-in size cap on `DROVER_LOG_DIR` itself. Per-file
rotation keeps individual files at or below `DROVER_LOG_MAX_FILE_BYTES`
(default 10 MiB), but a long-running noisy container will accumulate
many files. Monitor disk usage on the volume.

---

## 6. Disk-full behavior

When a write to a captured log file fails with `ENOSPC` (or any other
persistent OS-level write error), the orchestrator:

1. Logs one structured `ERROR`-level line to its own stdout, identifying
   the container that triggered the failure and the errno.
2. Sets an internal flag that disables disk writes for the rest of the
   orchestrator process's lifetime — for **all** containers, not just
   the one that failed. The Docker follow streams keep running, but the
   parsed records are dropped.

Auto-recovery on space recovery is intentionally not attempted; recovery
procedure is:

```bash
# 1. Free space on the volume that holds DROVER_LOG_DIR.
# 2. Restart the orchestrator container.
docker compose restart orchestrator
```

Live tails via `GET /containers/{id}/logs` continue working throughout
because they read directly from Docker.

---

## 7. Lifecycle and retention guarantees

| Container event | What happens to `{DROVER_LOG_DIR}/{id}/` |
|---|---|
| `initializing`, after Docker `start_container` succeeds | Directory created. `0.log` opened. Capture begins **before** the guest agent connects. This catches init-failure output. |
| `initializing` → `running` | No log-layer change; capture is already running. |
| `initializing` → `error` (init timeout, init Docker failure) | Capture stops cleanly. `.cursor` is persisted. Directory is **kept** so the operator can post-mortem the failed init. |
| `running` → `stopped` | Capture stops cleanly. `.cursor` is persisted. Directory is kept. |
| `stopped` → `running` (resume) | Capture restarts using `since=<.cursor>` to bridge the gap. New records append to the existing top file (or rotate if it's full). |
| any non-destroyed state → `destroyed` | Directory is removed. This applies to `error` and `initializing` rows too — a destroyed container has no logs. |
| Orchestrator restart | The startup sync re-opens a follow stream for each `running` row, using the persisted `.cursor` so the gap is small. May produce up to one second of duplicate records (Docker's `since=` parameter is integer-second precision). |

Permission note: the orchestrator runs as UID 1000. The default
Dockerfile creates `/var/lib/orchestrator` with that ownership, so
`DROVER_LOG_DIR=/var/lib/orchestrator/logs` works out of the box. If
you mount `DROVER_LOG_DIR` somewhere else, ensure UID 1000 can create
files and directories in it — the first-write failure manifests as the
disk-full path described in §6 and silently disables capture for the
rest of the process lifetime.

---

## 8. Live tail

`GET /containers/{id}/logs` proxies Docker's logs API directly and
returns `text/plain`. It does **not** read from `DROVER_LOG_DIR` in the
current implementation; it is purely a live tail of Docker's own
buffer. A follow-up plan will broaden this endpoint with consistent
pagination (`since`/`until`/`limit`/`offset`) and have it read from
disk when retention is enabled. Until then:

- Want the last 200 lines from the live container? `GET .../logs?tail=200`.
- Want the full retained history (including for stopped containers)?
  `GET .../logs/files`, then fetch each file under `/logs/files/{filename}`.

The two endpoints don't share data: the live tail is bounded by
whatever Docker's log driver retains, and the on-disk history is
bounded by destroy.
