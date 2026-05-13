# Full End-to-End Test Suite

This document describes the strategy and mechanics of Drover's end-to-end
test suite, which lives at [`/e2e`](../e2e). The suite builds the orchestrator,
webapp, and builder images from source, brings them up as a real
multi-container stack, and exercises the public HTTP API and log output to
catch regressions that the unit and integration tests can't see.

For a developer-focused "how do I run this?" overview, see [`e2e/README.md`](../e2e/README.md).

## Goals

1. Catch regressions across the full stack — orchestrator, webapp, builder,
   and the per-container socket protocol — before they reach `main`.
2. Be runnable locally with a single command, with a tight inner loop for
   iterating on one service at a time.
3. Run unattended on GitHub Actions runners, with enough log capture that
   a failure can be diagnosed from the uploaded artifact alone.

## Lifecycle stages

The suite is intentionally split into discrete lifecycle stages so a
developer's inner loop is `change one file → rebuild one container → re-run
tests`, not `wait for the whole stack to rebuild every time`.

There is a single entry point, `e2e/run.sh`, with these subcommands:

| Command | Purpose |
|---|---|
| `up` | Build all images from source, start the stack, wait for health. |
| `down` | Tear the stack down and drop its volumes. |
| `restart` | Rebuild and recreate all services. |
| `restart <service>` | Rebuild and recreate one of `orchestrator`, `webapp`, or `builder`. The tightest inner loop. |
| `test` | Run the test suite against an already-running stack. |
| `ci` | One-shot `up + test + down` for GitHub Actions. |

## Stack definition

Everything the stack needs is in `e2e/docker-compose.e2e.yml`:

- **orchestrator** — built from `orchestrator/Dockerfile`, exposed on port
  `8000`. Configured with a hard-coded API key (see below).
- **webapp** — built from `webapp/Dockerfile`, exposed on port `9091`. Talks
  to the orchestrator over the compose network.
- **builder** — built from `builder/Dockerfile`. Not a long-running service:
  the entry exists so that `docker compose build` produces the image with its
  `drover.managed=true` / `drover.name=builder` labels visible to the
  orchestrator's image discovery.

### Same-path socket bind-mount

The orchestrator creates per-container Unix sockets and asks the host Docker
daemon to bind-mount them into child containers. Docker resolves bind-mount
sources against the host filesystem, so the host path and the in-container
path must match. The compose file uses `/tmp/drover-sockets` for both ends.
`run.sh up` creates this directory with mode `0777` so the orchestrator user
(`uid 1000` inside its container) can write to it.

### API key

The test API key is hard-coded so the suite needs no per-run setup:

- Plain-text key: `drover-e2e-test-key`, stored in `e2e/.env.test` and used
  by the test scripts in the `Authorization: Bearer …` header.
- SHA-256 hex digest of the plain-text key, stored in
  `e2e/docker-compose.e2e.yml` as the orchestrator's `DROVER_API_KEY`
  environment variable. This is the value the orchestrator's auth middleware
  compares against.

Both halves are intentionally committed: the configuration only ever runs
against throwaway local or CI Drover stacks.

## Tests

Tests live under `e2e/tests/` as numbered bash scripts. The runner sources
them in lexical order and stops at the first failure.

| File | What it checks |
|---|---|
| `01-health.sh` | Both running services answer their `/health` endpoint with `{"healthy": true}`. |
| `02-images.sh` | The orchestrator discovers the builder image by its `drover.*` labels and returns it from `GET /images` and `GET /images/builder`. |
| `03-privileged-container.sh` | Full privileged-container lifecycle: create, poll to `running`, exec with an env var, assert exit code and stdout, stop, poll to `stopped`, and assert the orchestrator's JSON log is free of `level=ERROR` lines. |
| `04-standard-container.sh` | Same lifecycle as test 03 but for a non-privileged container, which the orchestrator hard-codes to run under the `runsc` runtime (gVisor). |

### The key test: 03-privileged-container

This single test exercises the socket protocol, state machine, and log
output in one pass:

1. `POST /containers` with the builder image, `privileged: true`, and the
   env var `DROVER_TEST_VAR=hello_drover`.
2. Poll `GET /containers/{id}` until `status == running`.
3. `POST /containers/{id}/exec` with `echo $DROVER_TEST_VAR`.
4. Poll the exec endpoint until `status == complete`; assert `exit_code == 0`
   and that the joined stdout messages contain `hello_drover`.
5. `POST /containers/{id}/stop`.
6. Poll until `status == stopped`.
7. Dump the orchestrator's container log and assert zero lines have
   `level == "ERROR"`.

It only depends on the privileged path, so it runs on any host with Docker —
gVisor is not required.

## gVisor strategy

Drover's orchestrator hard-codes the `runsc` runtime for non-privileged
containers (`container_manager.py`). Test 04 therefore needs gVisor on the
host. The suite handles this in three ways:

- **In GitHub Actions** — the e2e workflow installs `runsc` as a setup step.
  On `ubuntu-latest` this takes ~60 seconds and gives full-fidelity testing.
- **On a developer laptop with gVisor installed** — the suite uses it
  automatically.
- **On a developer laptop without gVisor** — the suite fails with a clear
  message telling you to install runsc *or* set
  `E2E_ALLOW_MISSING_RUNSC=1` to skip test 04 explicitly. Silent skips are
  off by default because the failure mode they create ("the test is green
  but it didn't actually run") is exactly what an e2e suite is supposed to
  prevent.

There is **no** `DROVER_REQUIRE_GVISOR` toggle in the orchestrator.
Production-code complexity is reserved for production-code problems, and
CI can install gVisor, so there is no need for the orchestrator to be
aware of the test environment.

### Required: `--host-uds=all` runtime flag

After `runsc install`, the `runsc` runtime entry in `/etc/docker/daemon.json`
must include `--host-uds=all` in its `runtimeArgs`. Without this flag, gVisor
blocks the guest agent from connecting to the per-container orchestrator
socket that is bind-mounted into the container, and every non-privileged
container times out in `initializing` and fails with `init_timeout`.

The flag is safe in context: it permits Unix socket connections only to
paths accessible through the container's own mount namespace, which for a
non-privileged Drover container is exactly the one per-container
orchestrator socket. The Docker socket is not bind-mounted into
non-privileged containers, so it remains unreachable.

```json
"runtimes": {
  "runsc": {
    "path": "/usr/local/bin/runsc",
    "runtimeArgs": ["--host-uds=all"]
  }
}
```

This applies equally to developer laptops and CI runners. See
[`install-runsc-gvisor.md`](./install-runsc-gvisor.md) for the broader
gVisor install guide.

## Log strategy

Drover's operations are asynchronous: `POST /containers` returns immediately
with `status: initializing`, but the real work — socket creation, `docker
run`, the guest agent connecting and sending `ready` — happens in background
tasks over the next several seconds. Logs relevant to a given API call are
emitted *after* the HTTP response. The capture window must therefore span
from before the HTTP request through to when the background work reaches a
terminal state.

### Timing model

Each logical test step records two timestamps:

- **T1** — captured *before* the HTTP request is sent, to catch any
  pre-flight orchestrator activity.
- **T2** — captured *after* the terminal state is confirmed via polling,
  plus a ~500 ms grace buffer (configurable via `E2E_LOG_GRACE_MS`)
  because some log lines emit shortly after the DB write that flips state.

Then `docker logs <container> --since T1 --until T2+grace` gives the exact
window for that step. T1 and T2 are formatted as RFC3339 with an explicit
`Z` so they're interpreted as UTC regardless of the host's local timezone.

### Log chunk files

Each logical step produces one chunk file under
`e2e/logs/<run-id>/<test>/<NN-step>.log`. The chunk is plain text with
labelled sections, readable by humans and automated systems alike:

```
STEP:      create-container
TEST:      03-privileged-container
RUN_ID:    2026-05-12T14-30-00Z
STARTED:   2026-05-12T14:30:01.234Z
COMPLETED: 2026-05-12T14:30:05.923Z
RESULT:    PASS

--- REQUEST ---
POST http://localhost:8000/containers
{"image": "builder", "privileged": true, "env": {"DROVER_TEST_VAR": "hello_drover"}}

--- RESPONSE ---
HTTP 201
{"id": "abc123def", "status": "initializing", ...}

--- WAIT ---
target_status: running
timeout: 30s
elapsed: 4689ms
polls: 4

--- EXEC RESULT ---           (only present for exec steps)
exit_code: 0
stdout: hello_drover
stderr: (none)

--- ORCHESTRATOR LOGS ---
{"ts":"2026-05-12T14:30:01.412Z","level":"INFO","msg":"POST /containers 201"}
...

--- WEBAPP LOGS ---
(none in window)

--- FAILURE ---
(none)
```

Exec output (stdout/stderr/exit code) travels over the per-container Unix
socket and is returned via the API — it does not appear in orchestrator
logs. Both sources appear as separate sections because they cannot be
accurately interleaved by time.

The chunk-file format intentionally captures only the orchestrator and
webapp logs per step. Logs from the *micro-containers* that the
orchestrator spawns are not interleaved here — see the next section for
why and where they end up instead.

### Micro-container logs

Every `POST /containers` call causes the orchestrator to spawn a real
Docker container — the *micro-container* that runs the guest agent and
whatever work the caller exec's into it. Its stdout and stderr (the
guest agent's output, plus anything an exec'd command printed) are just
as important to debugging as the orchestrator's own logs: they're the
only window into what the guest agent saw, which is often where socket
errors, gVisor misconfiguration, or guest-side crashes show up.

These logs are **not** interleaved into the per-step chunk files. There
are two reasons:

1. **No reliable per-step window.** A single test step can span several
   exec calls against one micro-container, and a single micro-container
   may outlive several test steps. The chunk file's `T1 → T2+grace`
   window — which is meaningful for the orchestrator and webapp because
   those are long-lived services — doesn't have a sensible
   interpretation for a micro-container that started mid-test.
2. **No Docker ID exposed to the test scripts.** The orchestrator's
   public API identifies containers by its own generated ID, not by the
   Docker ID. Filtering `docker logs <docker-id>` per step would require
   either an API change to surface `docker_id` or a side-channel query
   into the orchestrator's database — both heavier than the problem
   warrants.

So instead, `./e2e/run.sh ci` does a holistic post-mortem dump just
before tear-down: every Docker container carrying the
`drover.managed=true` label (which the builder image bakes in via its
Dockerfile, and which every spawned container inherits) has its full
`docker logs` output written to:

```
e2e/logs/microcontainers/<docker-id>.log
```

Each file starts with a small header naming the Docker ID, the image,
and the auto-generated Docker container name, followed by the raw
container log. The orchestrator's own log lines mention the
micro-container's Docker ID whenever they interact with it (search for
`docker=<short-id>`), so a developer can grep an orchestrator log line
for that short ID and immediately find the matching micro-container
file in this directory.

The micro-container logs are uploaded as part of the standard
`e2e/logs/` artifact when the workflow fails.

### Run directory layout

```
e2e/logs/
├── orchestrator.log                  # Full container log (ci mode only)
├── webapp.log                        # Full container log (ci mode only)
├── microcontainers/                  # Full container log per spawned micro-container (ci mode only)
│   ├── <docker-id>.log
│   └── …
└── 2026-05-12T14-30-00Z/             # RUN_ID
    ├── summary.log                   # One line per step, with first-failure pointer
    ├── 01-health/
    │   ├── 01-orchestrator-health.log
    │   └── 02-webapp-health.log
    ├── 02-images/
    │   └── …
    └── 03-privileged-container/
        ├── 01-create-container.log
        ├── 02-exec-command.log
        ├── 03-stop-container.log
        └── 04-assert-no-errors.log
```

`summary.log` is the entry point for automated systems: it identifies the
first failure and names the chunk file to inspect. Each chunk file is
fully self-contained for diagnosis.

The top-level `orchestrator.log`, `webapp.log`, and `microcontainers/`
directory are only written by `./e2e/run.sh ci`, which captures full
container logs to disk just before tearing the stack down. That gives
the CI workflow something to print and upload as an artifact after the
containers are gone — `docker logs` would otherwise fail with "no such
container" in the post-teardown steps.

### Library design

In `e2e/lib/logs.sh`, two functions bracket each step:

```bash
step_begin "create-container"
# … API call + polling …
step_end   # records T2, pulls logs from T1→T2+grace, writes the chunk
```

A bash `ERR` trap (enabled with `set -E`) ensures that even unexpected
failures — `curl` returning no output, `jq` choking on malformed JSON —
write a partial chunk file before exiting, instead of leaving `logs/` in
an ambiguous state. The partial chunk includes everything captured up to
the point of failure plus the failure reason in the `FAILURE` section.

On GitHub Actions, the entire `e2e/logs/` directory is uploaded as an
artifact on failure (see [`.github/workflows/e2e.yml`](../.github/workflows/e2e.yml)).

## GitHub Actions

The `e2e` workflow at [`.github/workflows/e2e.yml`](../.github/workflows/e2e.yml)
is `workflow_dispatch`-only for now. It can be promoted to run automatically
on pull requests targeting `main` once the suite is stable and fast enough.

Its steps, in order: checkout → install gVisor (with `--host-uds=all`) →
ensure `jq` is present → set up Buildx → `./e2e/run.sh ci` → upload
`e2e/logs/` as an artifact on failure. Total wall-clock time on a fresh
`ubuntu-latest` runner is approximately 3–4 minutes.

## Webapp testing (phased)

- **Phase 1 (current)** — test 01 validates the webapp's `/health` endpoint
  via curl. That's sufficient for the initial suite.
- **Phase 2 (later)** — add `e2e/playwright/` with Playwright tests living
  alongside the bash tests, triggered separately (either from
  `run.sh test --browser` or their own script). Adding Playwright later
  doesn't require restructuring anything in phase 1.
