# Drover end-to-end tests

A bash-based test suite that builds the orchestrator, webapp, and builder
images from source, brings them up as a real multi-container stack, and
asserts the full lifecycle works end to end.

For the full design — log capture, timing model, gVisor requirements,
chunk-file format — see [`docs/full-e2e-suite.md`](../docs/full-e2e-suite.md).

## Prerequisites

- Docker with Compose v2 (`docker compose`).
- `jq` and `curl` on `PATH`.
- For test 04 (non-privileged containers under gVisor): `runsc` installed
  and registered in Docker with `--host-uds=all` — see
  [`docs/install-runsc-gvisor.md`](../docs/install-runsc-gvisor.md). If
  you genuinely want to skip test 04, set `E2E_ALLOW_MISSING_RUNSC=1`;
  otherwise the test fails loudly so missing gVisor isn't mistaken for a
  green run.

## Run everything (fresh stack, like CI)

```bash
./e2e/run.sh ci
```

That builds all images, brings the stack up, runs every test, and tears
the stack back down.

## Inner-loop workflow

If you're iterating on a single service, run the stack once and then
rebuild + retest just that piece:

```bash
./e2e/run.sh up                        # one-time: build + start everything
./e2e/run.sh restart orchestrator      # after editing orchestrator code
./e2e/run.sh test                      # re-run tests against the live stack
./e2e/run.sh down                      # when you're done
```

`restart` accepts `orchestrator`, `webapp`, or `builder`. With no
argument it rebuilds and restarts all services.

## Where logs go

Every `./e2e/run.sh test` invocation creates a fresh run directory under
`e2e/logs/<run-id>/` containing:

- `summary.log` — one line per step, plus a final `RESULT:` marker.
- One subdirectory per test (e.g. `03-privileged-container/`) holding a
  self-contained chunk file per step.

`e2e/logs/` is gitignored. On GitHub Actions it's uploaded as an artifact
when the workflow fails.

## Configuration

The suite uses a deterministic, hard-coded API key (plain text in
`e2e/.env.test`, SHA-256 in `docker-compose.e2e.yml`) so it needs no
per-run setup. Only edit those files if you have a specific reason to.

Two optional environment variables:

| Variable | Purpose | Default |
|---|---|---|
| `E2E_ALLOW_MISSING_RUNSC` | Set to `1` to explicitly skip test 04 when gVisor isn't installed. | unset (test fails loudly if `runsc` is missing) |
| `E2E_LOG_GRACE_MS` | Milliseconds appended to each step's log-capture window so trailing log lines aren't truncated. | `500` |
