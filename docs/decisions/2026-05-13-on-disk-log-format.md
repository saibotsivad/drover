# ADR: On-disk container-log format

**Date:** 2026-05-13
**Status:** Accepted

## Context

The container-log-retention feature writes captured micro-container
stdout/stderr to disk under `/var/lib/drover/logs/` when
`DROVER_ENABLE_CONTAINER_LOGS=true` (see the sibling ADR
`2026-05-13-container-log-capture-architecture.md` for the capture
pipeline). We had to pick:

1. The line format (per-line schema and serialization).
2. Whether to split that schema's payload at every newline (matching
   Docker's `json-file` driver exactly) or emit one record per Docker
   log frame regardless of how many newlines it contains.

Both choices are downstream-visible: every operator who points
Promtail / Vector / Fluent Bit / Loki at `/var/lib/drover/logs/` will reverse-
engineer the format from one of our sample files. Once shipped, we can
only change it via a versioned migration.

## Decision

**Line format: Docker `json-file` driver, verbatim.** One JSON object
per line, newline-delimited, with keys `log` (string), `stream`
(`"stdout"` or `"stderr"`), and `time` (RFC3339Nano string). No
Drover-specific fields.

**Splitting: one record per Docker frame, not per newline.** When a
container's PID 1 calls `write(2)` with a buffer that contains
embedded newlines, we emit a single `{log, stream, time}` record whose
`log` field contains those newlines. We do not re-split the payload at
each `\n` to produce one record per logical line.

## Consequences

### Positive

- **Free interoperability.** Every existing Docker log shipper
  recognizes the format out of the box. Promtail's `docker` pipeline
  stage, Vector's `docker_logs` source, Fluent Bit's `docker` parser,
  and Loki's docker-driver all consume the same files. Operators who
  already ship Docker's own `json-file` logs ship ours the same way.
- **Trivial to add streaming-history endpoints later.** The follow-up
  pagination plan needs files ordered by name and timestamps in each
  record. Both are present; no migration needed when that work lands.
- **Implementation cost is low.** Avoiding the split removes a parsing
  step, removes the question of how to split malformed UTF-8 inside a
  frame, and keeps the writer's hot path simple (one `json.dumps`,
  one `write`, one `flush`).

### Negative

- **The `log` field can contain `\n`.** Operators wiring downstream
  parsers that assume "one record == one line of program output" will
  see multi-line `log` fields and need a `multiline` stage (or
  equivalent). This is documented in `docs/observability.md` so it
  isn't a surprise. We accept this divergence from `json-file`'s
  exact behavior because:
  - Docker's frame boundaries are already where `write(2)` calls
    happened, so each frame is a single logical "thing the program
    decided to emit." Splitting at `\n` invents boundaries that the
    program did not.
  - Re-splitting requires opinion about UTF-8 handling, trailing-
    newline semantics, and whether to emit zero-length records for
    blank lines. Avoiding the split avoids those opinions.

- **The format is somewhat coupled to Docker.** If we ever swap the
  capture path away from Docker's `/containers/{id}/logs?follow=1`
  stream — for example, reading the daemon's own log files directly —
  we still want to emit the same on-disk format because that's what
  shippers expect. The decision lives at the writer layer, not at the
  source layer, so this is fine.

## Alternatives Considered

- **A Drover-specific schema** (`{ts, lvl, container_id, source,
  payload}`). Rejected because every shipper would need a custom
  parser, and we add nothing semantic the operator can't enrich
  themselves from the directory structure.
- **Match `json-file` exactly, including the per-line split.**
  Rejected because the parsing is non-trivial (binary payloads,
  UTF-8 boundaries, trailing-newline edge cases) and the deviation
  is straightforwardly documented for shippers that care.
- **Multiplexed binary format identical to Docker's wire format.**
  Rejected because it requires a custom parser even to `cat` the file,
  defeating the point of "operator can read the directory directly."

## References

- `docs/decisions/2026-05-13-container-log-capture-architecture.md` —
  sibling ADR about the capture pipeline.
- `docs/observability.md` — operator-facing reference, including the
  `log`-field-may-contain-newlines caveat for shippers.
- `orchestrator/log_capture.py` — the writer implementation.
