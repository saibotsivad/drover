# ADR: Container-log capture architecture

**Date:** 2026-05-13
**Status:** Accepted

## Context

Drover needs to retain micro-container stdout/stderr beyond the
lifetime of the underlying Docker container, so operators can debug
containers that have stopped or errored. The decision space:

- How the orchestrator gets at the container's output.
- What "retention" actually means — what file survives `docker rm`.
- Whether Drover takes a dependency on the operator's existing log
  infrastructure or replaces it.

Constraints we accepted going in:

- **No required external stack.** A homelab operator should get useful
  retention with nothing else installed. Loki/Grafana/journald are
  options the operator may add, never preconditions.
- **No new heavy runtime dependencies.** The orchestrator container
  ships FastAPI/httpx/aiosqlite/uvicorn; pulling in Vector or Fluent
  Bit was off the table.
- **Rootless Docker compatible.** UID 1000 inside the orchestrator
  must work with what it can reach via the Docker socket; reading
  daemon-internal files off the host is fragile under rootless.
- **Survives orchestrator restart.** Capture must resume without
  losing logs after the orchestrator container itself restarts.

The on-disk format is decided in the sibling ADR
`2026-05-13-on-disk-log-format.md`; this ADR is about the capture
*pipeline* and Drover's relationship to the operator's log stack.

## Decision

For each running micro-container, the orchestrator opens a follow
stream against Docker's logs API
(`GET /containers/{docker_id}/logs?follow=1&stdout=1&stderr=1&timestamps=1&since=...`),
parses the multiplex frames, and appends them to a per-container
directory under `/var/lib/drover/logs/` (the fixed in-container log
path; enabled by `DROVER_ENABLE_CONTAINER_LOGS=true`). A `.cursor`
file in that directory
holds the timestamp of the most recently written record so the stream
can resume after orchestrator restart.

Capture begins immediately after `docker start` succeeds — *before*
the guest agent connects — so init-failure stdout is captured.

When `DROVER_ENABLE_CONTAINER_LOGS` is unset (or set to anything other
than the exact string `"true"`), no capture happens at all and
operators rely on whatever Docker's daemon log driver retains.

Drover never sets `HostConfig.LogConfig` on container creation. The
daemon's log-driver choice (`json-file`, `journald`, `loki`, …) is
independent of Drover's capture and unaffected by it.

## Consequences

### Positive

- **Standalone usability.** A homelab operator with nothing else
  installed gets working retention by reading the capture directory.
- **Resume after restart.** The `.cursor`-backed `since=` resume
  bridges the gap between orchestrator processes; documented behavior
  prefers duplicate records to gaps.
- **Compatible with rootless Docker.** We talk to the Docker daemon
  via its socket the same way every other Drover call does; we do not
  read daemon-internal files off the host.
- **Operator keeps full control of the daemon's log driver.** Loki,
  journald, fluentd — whatever is configured at the daemon level keeps
  applying, totally independent of Drover.

### Negative

- **Two copies of every byte by default.** When
  `DROVER_ENABLE_CONTAINER_LOGS=true` and the daemon also writes (the
  `json-file` default), every
  container's stdout exists twice. `docs/observability.md` §5 walks
  the operator through disabling one or the other.
- **One follow-stream task per running container.** Cheap (asyncio
  task + buffered I/O) but it is a per-container resource that scales
  linearly with container count.
- **Capture lags if the asyncio loop is blocked.** Docker streams in
  real time as long as we hold the connection; if the orchestrator
  event loop is starved, captured records arrive late. We treat this
  as an orchestrator-health concern, not a logging concern.

## Alternatives Considered

### A. Read Docker's `json-file` files directly off the host

Mount `/var/lib/docker/containers/` (or the rootless equivalent)
read-only into the orchestrator and read each container's
`<docker_id>-json.log` directly.

Rejected because:

- Couples Drover to a specific daemon log driver. If the operator runs
  `--log-driver=journald` (common), the file does not exist.
- Rootless Docker stores under `~/.local/share/docker/`; the path
  varies and is not guaranteed to be readable by UID 1000 inside the
  orchestrator container.
- Docker deletes those files on `docker rm`, which is exactly when we
  still want them. We'd still need a copy-out step before destroy.
- Bypasses the Docker API for filesystem layout that is technically
  internal.

### B. Snapshot logs via the non-streaming logs API only at stop time

Skip live capture; when a container transitions to `stopped`, call
`GET /containers/{id}/logs` once, dump the result, done.

Rejected because:

- A long-running noisy container can exhaust Docker's own log driver
  buffer (default 10 MiB rotated × 5 with `json-file`) before we read
  it. Earlier output is gone by the time we snapshot.
- An OOM-killed or `kill -9`'d container that the orchestrator missed
  produces partial logs (Docker may have already rotated).
- The work saved is small: opening a follow stream and appending to a
  file is barely more code than a one-shot logs call.

### C. Force `--log-driver=local` per container and shell out to `docker logs --tail`

Reuse Docker's own retention; treat Drover as a thin wrapper around
`docker logs`.

Rejected because:

- Same retention problem: `docker rm` deletes the logs. We still need
  to copy them out before destroy.
- Forcing a log driver fights any operator who has globally configured
  a different one (e.g. `--log-driver=loki`). Drover should not
  override the host daemon's policy.
- Removes the operator's freedom to fan their micro-container stdout
  out to their own stack, which is one of the explicit goals.

### D. Bundle a "Drover ships with Loki/Grafana" stack

Provide a docker-compose profile with Loki/Promtail/Grafana wired up
and recommend it as the supported logging path.

Rejected because:

- Demands the operator install and maintain a logging stack they may
  not want, just to get basic log retention. Drover is meant to work
  standalone in a homelab.
- The choice of Loki vs Grafana Cloud vs Elasticsearch vs journald is
  operator-specific. Hard-wiring an opinion ages poorly.
- We can document the Loki path *as an example* without bundling it,
  and `docs/observability.md` §4 does exactly that.

## References

- `docs/observability.md` — operator-facing reference for the feature.
- `docs/decisions/2026-05-13-on-disk-log-format.md` — sibling ADR
  about the on-disk line format and the per-frame-vs-per-line split.
- `orchestrator/log_capture.py` — the writer implementation.
- `orchestrator/container_manager.py` — lifecycle hooks (`_init_container`,
  `_fail_init`, `stop_container`, `resume_container`,
  `destroy_container`, `sync_containers`).
