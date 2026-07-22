# Drover

Drover is a container orchestration tool for homelab work. You run a single
long-lived **orchestrator** that exposes a REST API, and through that API
you launch **ephemeral micro-containers** on demand: lightweight, isolated
Linux environments that spin up to do a job and are stopped or destroyed
when they're done.

Think of it as a function-as-a-service where the "functions" are whole
micro Linux operating systems. A caller asks the orchestrator to launch a
named image, sends it commands, streams back the output, and lets the
orchestrator handle every lifecycle detail in between. As with AWS Lambda,
callers never touch Docker directly and arbitrary images are not
permitted — only specifically labeled images installed on the host are
available to launch.

## Concepts

| Term | Definition |
|---|---|
| **Host** | Bare-metal machine running Docker in [rootless](https://docs.docker.com/engine/security/rootless/) mode. |
| **Orchestrator** | The long-lived Docker container that manages the micro-container fleet and serves the API. |
| **Micro-container** | A short-lived, ephemeral container launched and managed by the orchestrator. |
| **Privileged micro-container** | A micro-container with access to the host Docker socket, used for build and setup tasks. |

## How it works

The orchestrator is the core of Drover. It runs as a Docker container on
the host and exposes a REST API to create, command, stop, resume, and
destroy micro-containers. Each micro-container is an instance of an
operator-provided image, launched on demand, communicated with over a Unix
socket, and stopped or destroyed when no longer needed.

```
Host (bare metal, rootless Docker)
└── orchestrator container
    ├── REST API      ←  callers (CLI, webapp, scripts, CI)
    ├── Docker client →  creates / starts / stops micro-containers
    └── Unix sockets  ↔  guest agents inside micro-containers
```

A few design choices shape the whole system:

- **Direct Docker Engine API.** The orchestrator talks to the Docker daemon
  straight over the mounted Unix socket using `httpx` — there is no Docker
  Python SDK. This keeps the dependency tree minimal and gives full control
  over the API calls. It is built on FastAPI/Uvicorn with `aiosqlite` for
  async state.
- **Isolation by default.** Standard micro-containers run under
  [gVisor](https://gvisor.dev/) (`--runtime=runsc`) for syscall
  interception; only explicitly privileged containers bypass it to reach
  the host Docker socket.
- **Label-based image discovery.** The orchestrator only launches images
  that carry the `drover.*` labels, so arbitrary images can never be run.
- **Socket protocol.** Each micro-container gets a per-container Unix socket
  carrying newline-delimited JSON. A guest agent inside the container
  connects once, signals readiness, sends heartbeats, and streams command
  output back. The orchestrator tracks each container through a lifecycle
  state machine and reaps idle ones automatically.

For the full picture — the REST API reference, the lifecycle state machine,
mounts, and the on-the-wire socket protocol — see the
[orchestrator README](orchestrator/README.md). The initialization/resume
handshake and the exec command flow are documented in
[docs/container-initialization.md](docs/container-initialization.md) and
[docs/exec-commands.md](docs/exec-commands.md).

## Components

Drover is a single repository of independently-versioned components:

| Component | What it is | Docs |
|---|---|---|
| **Orchestrator** | The core service: REST API, Docker orchestration, state, socket routing. | [orchestrator/README.md](orchestrator/README.md) |
| **CLI** (`drover`) | Single-binary command-line client for the API. | [docs/cli.md](docs/cli.md) · [cli/README.md](cli/README.md) |
| **Executor** | Python guest-agent library that runs inside micro-containers. | [executor/README.md](executor/README.md) |
| **Webapp** | Optional htmx-based management UI that fronts the orchestrator. | [webapp/README.md](webapp/README.md) |
| **Builder** | Reference privileged image (executor + Docker CLI + git) for building images. | [builder/README.md](builder/README.md) |

## Installation

### Requirements

To run Drover, the host needs:

1. **Docker in rootless mode**, running as the operator user.
2. **The `runsc` (gVisor) runtime** registered with Docker, so standard
   micro-containers can be isolated. See
   [docs/install-runsc-gvisor.md](docs/install-runsc-gvisor.md).
3. **The orchestrator container**, started with the mounts described in the
   [orchestrator README](orchestrator/README.md#mounts).
4. **At least one micro-container image** carrying the required Drover labels
   so the orchestrator has something to launch (see
   [Images](#images-and-builders)).
5. *(Optional)* **A privileged image** on the host — required only if you
   plan to use privileged micro-containers for builds.

### Run the orchestrator

The quickest path is the pinned, signed compose stack attached to each
release. Download it and bring the stack up:

```sh
curl -fsSL -O https://github.com/saibotsivad/drover/releases/latest/download/docker-compose.yml
docker compose up -d
```

Every image reference in the released compose file is pinned by digest, so
a `compose up` is byte-identical to what was tested at release time. The
[sample `docker-compose.yml`](docker-compose.yml) in this repo shows the
recommended host bindings and can be used as a starting point for a custom
setup. For the full release/verification story see
[docs/releases.md](docs/releases.md).

### Install the CLI

```sh
curl -fsSL https://github.com/saibotsivad/drover/releases/latest/download/install.sh | sh
```

The installer detects your OS/arch, verifies the binary's checksum, and
installs `drover` to `/usr/local/bin`. Supply-chain-conscious users can
verify the signed installer first — see
[docs/releases.md#installation](docs/releases.md#installation).

## Using Drover

Point the CLI at your orchestrator and authenticate through two environment
variables:

```sh
export DROVER_API_URL=https://drover.example.com
export DROVER_API_KEY=sk-...     # the plain-text key; omit if auth is disabled
```

A minimal end-to-end flow — launch a container, run a command, tear it
down:

```sh
drover images                                   # what can I launch?
id=$(drover start python-runner --env FOO=bar | jq -r '.id')
drover exec "$id" -- echo "hello from a micro-container"
drover stop "$id"                               # resumable
drover destroy "$id"                            # permanent
```

Every command prints JSON to stdout so it composes with `jq`. The full
command, flag, and exit-code reference lives in
[docs/cli.md](docs/cli.md). You can also drive everything from the browser
with the optional [webapp](webapp/README.md), or call the REST API directly
(see the [orchestrator API reference](orchestrator/README.md#api-reference)).

## Operating your install

Once Drover is running, these are the levers you'll reach for as a
maintainer:

- **Configuration.** The orchestrator is tuned entirely through environment
  variables — auth, the privileged image, timeouts, the idle reaper, log
  capture, and log level. The full table is in the
  [orchestrator README](orchestrator/README.md#configuration).
- **Authentication.** Bearer-token auth is optional and off by default. When
  enabled, every request except `GET /health` requires a token. See
  [docs/authentication.md](docs/authentication.md) for key generation and
  setup.
- **Observability.** Orchestrator logs are structured JSON on stdout;
  micro-container stdout/stderr can optionally be captured to disk
  (`DROVER_ENABLE_CONTAINER_LOGS=true`) so history survives restarts. The
  retention model and log-shipper recipes are in
  [docs/observability.md](docs/observability.md).

### Images and builders

Micro-containers launch from images that carry the `drover.managed` and
`drover.name` labels; the orchestrator ignores everything else on the host.
Building and labeling those images — including how to label a pre-built
upstream image — is covered in
[docs/image-management.md](docs/image-management.md). Which capability-gated
features an image supports is advertised via the `drover.capabilities`
label, documented in [docs/capabilities.md](docs/capabilities.md).

For build/setup workloads, point `PRIVILEGED_IMAGE` at a privileged image
such as the reference [builder](builder/README.md), which bundles the guest
agent with the Docker CLI and git.

## Development

### Testing

The test suite is split into independent runs:

- **Orchestrator tests** (`tests/`) — unit tests for ID generation, config,
  models, database, and the container-manager state machine:

  ```sh
  pytest tests/ -v
  ```

- **Executor tests** (`executor/tests/`) — guest-agent wire protocol,
  subprocess streaming, and full agent lifecycle against mock socket
  servers:

  ```sh
  pytest executor/tests/ -v -p no:asyncio -p no:anyio
  ```

- **End-to-end suite** (`e2e/`) — builds every image, brings up a real
  multi-container stack, and asserts the full lifecycle. See
  [e2e/README.md](e2e/README.md).

The `test.yml` GitHub Actions workflow runs the unit suites on every PR
alongside a Docker build and a `/health` smoke test.

### Versioning and releases

Each component is versioned independently, driven by human-authored change
files. To bump a version, drop a YAML file under
[`changes/`](changes/README.md) describing which project changed and how.
On merge to `main`, a workflow rolls all pending bumps into a single release
PR; merging that pushes per-component git tags and publishes images. The
umbrella CalVer release then cross-links every component version into a
signed manifest.

- Per-component flow and change-file format: [docs/versioning.md](docs/versioning.md)
- Umbrella releases, manifest, and install assets: [docs/releases.md](docs/releases.md)

### Design decisions

Significant architectural choices are recorded as ADRs under
[docs/decisions/](docs/decisions/README.md).

## Roadmap

Remaining work and open design decisions are tracked in [TODO.md](TODO.md).
