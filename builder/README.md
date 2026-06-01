# drover-builder

A reference **privileged worker** image for Drover. It bundles
the [`drover-executor`](../executor/README.md) worker agent with the
Docker CLI and git, so the orchestrator can drive it through the
standard socket protocol to build, pull, tag, and push container images
on the host Docker daemon.

This image is the operator's "build slave": it's the thing you point
`PRIVILEGED_IMAGE` at when you want Drover to be able to construct new
worker images for itself.

Published as `ghcr.io/saibotsivad/drover-builder`.

---

## What's inside

| Component | Purpose |
|---|---|
| `python:3.12-slim` | Base image; runs the worker agent. |
| `drover-executor` | Reference worker agent. Connects to `/var/run/drover/sockets/orchestrator.sock`, runs commands as subprocesses, streams stdout/stderr back, reports exit codes. Installed from `/executor` in this repo. |
| `docker-ce-cli` | Talks to the bind-mounted host Docker daemon at `/run/docker.sock`. Use `docker build`, `docker pull`, `docker push`, `docker tag`, `docker image ls`, etc. |
| `docker-buildx-plugin` | Modern image builds via `docker buildx build` (BuildKit). |
| `git` | Clone source repos to build from. |

There is no Drover-specific Python in this image — all of the agent
logic lives in the `drover-executor` library. The builder image is
just the standard worker agent plus the host-Docker tooling.

### Drover labels

The image carries the labels required by the orchestrator's image
discovery:

```
drover.managed=true
drover.name=builder
```

That means the same image can be used in two ways:

- **As a privileged image.** Set `PRIVILEGED_IMAGE=ghcr.io/saibotsivad/drover-builder:latest` on the orchestrator and request privileged workers via the API. Privileged workers bypass gVisor and get the host Docker socket bind-mounted at `/run/docker.sock`.
- **As an ordinary managed image.** It shows up in `GET /images` as `builder` and can be launched the normal way (no Docker socket, gVisor enforced) — useful for read-only inspection or experimenting with the executor protocol.

The privileged path is what makes it a *builder* — without
`/run/docker.sock` it can't actually build anything.

---

## Building locally

The build context is the **repo root**, not `./builder`, because the
Dockerfile pulls the executor source in via `COPY executor/ ...`.
Build from the repo root and pass the Dockerfile explicitly:

```
docker build -f builder/Dockerfile -t ghcr.io/saibotsivad/drover-builder:dev .
```

The executor that gets installed is whatever's in your working tree at
`executor/`, so you can iterate on the agent and rebuild the builder
image without committing or pushing anything first. The repo's
top-level `.dockerignore` trims `.git/`, `node_modules/`, and similar
noise out of the build context so this stays fast.

To build from a specific tag instead of your working tree, check out
that tag first (`git checkout builder-v0.1.0` etc.) and rebuild.

---

## Running it via the orchestrator

The builder is not a long-running service — the orchestrator launches
it on demand. To make it available:

1. **Pull or build the image on the host.**

   ```
   docker pull ghcr.io/saibotsivad/drover-builder:latest
   ```

   The repo's `docker-compose.yml` has a `builder` service entry whose
   only job is to make `docker compose pull` / `docker compose up`
   fetch this image. It runs `command: ["true"]` and exits — the
   orchestrator launches real instances later.

2. **Point the orchestrator at it.** Set `PRIVILEGED_IMAGE` in the
   orchestrator's environment:

   ```yaml
   services:
     orchestrator:
       environment:
         PRIVILEGED_IMAGE: ghcr.io/saibotsivad/drover-builder:latest
   ```

3. **Request a privileged worker.** Send a `POST /workers` with
   `"privileged": true`:

   ```json
   {
     "image": "builder",
     "privileged": true,
     "label": "build-app-v3",
     "timeout_seconds": 600
   }
   ```

   The orchestrator creates the worker, mounts the host Docker
   socket at `/run/docker.sock` and the per-worker orchestrator
   socket folder at `/var/run/drover/sockets/`, and starts it. The
   `drover-executor` agent connects, sends `{"type": "ready"}`, and
   the worker transitions to `running`.

4. **Send build commands.** Once running, drive it through the normal
   exec API:

   ```
   POST /workers/{id}/exec   { "exec": "git clone https://github.com/me/my-app /src" }
   POST /workers/{id}/exec   { "exec": "docker build -t my-app:latest /src" }
   POST /workers/{id}/exec   { "exec": "docker push my-app:latest" }
   ```

   Each command is run as a subprocess by the executor; stdout, stderr,
   and the exit code are streamed back over the orchestrator socket
   and surfaced through the orchestrator's exec polling endpoint.

5. **Stop it.** Either send `POST /workers/{id}/stop`, let the idle
   timeout reap it, or send `DELETE` to stop and remove. There's no
   need to send a `done` message from inside the worker — the agent
   doesn't know when "the build" is finished, only the caller does.

---

## Customising

Three reasonable extension points, in order of intrusiveness.

### 1. Use a different executor version

Check out the ref you want and rebuild — the Dockerfile installs from
the local `executor/` folder, so the executor that gets baked in
tracks your working tree:

```
git checkout executor-v0.2.0
docker build -f builder/Dockerfile -t my-org/drover-builder:executor-0.2.0 .
```

### 2. Add extra tooling

Extend the published image and layer your tools on top:

```dockerfile
FROM ghcr.io/saibotsivad/drover-builder:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        make \
        rsync \
        skopeo \
    && rm -rf /var/lib/apt/lists/*
```

The Drover labels and the default `CMD` are inherited unchanged, so the
orchestrator still recognises the derived image and the executor still
runs on startup.

### 3. Run a custom agent

For workflows where you want the worker to drive itself (e.g. clone,
build, push, then exit) rather than waiting for exec commands, swap the
default `CMD` for a Python script that subclasses `Agent` and calls
`send_done()` when finished. See [Custom Agents](../executor/README.md#custom-agents)
in the executor docs for the full hook surface.

```dockerfile
FROM ghcr.io/saibotsivad/drover-builder:latest

COPY my_build_agent.py /app/my_build_agent.py
CMD ["python", "/app/my_build_agent.py"]
```

---

## Versioning

This image is one of Drover's [versioned projects](../docs/versioning.md).
Bump it by adding a YAML file under [`/changes`](../changes/) referencing
`project: builder`; the release workflow rolls all pending bumps into a
release PR that pushes a `builder-v<version>` git tag and publishes the
image to GHCR as `ghcr.io/saibotsivad/drover-builder:<version>`.
