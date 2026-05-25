# Interactive Exec (`drover exec <container-id>`)

**Date:** 2026-05-25  
**Status:** Approved — ready for implementation planning

## Context

Non-interactive exec (`drover exec <container-id> -- <command>`) is working:
the CLI creates an exec record via REST, opens a WebSocket for output
streaming, and the guest executor runs the command and sends back results.

The CLI already has a stub in `exec.go` that returns
`interactive_exec_unsupported` when `--` is absent.  This document
describes the agreed implementation approach.

## Goal

`drover exec <container-id>` (no `--`) opens a full interactive terminal
session: raw PTY I/O, terminal resize support, and a clean exit code on
shell exit.

## Approach — Docker exec API with PTY

The Docker Engine API supports creating an exec instance with
`AttachStdin/AttachStdout/AttachStderr + Tty: true`, then attaching
to get a raw PTY stream.  The orchestrator proxies bytes directly
between that Docker socket connection and a WebSocket.  PTY support
under `runsc` (gVisor) has been validated.

**Flow:**
```
CLI (raw mode) <--WS--> Orchestrator <--Docker socket--> Docker exec (PTY)
                                            |
                                   Cmd: /var/run/drover/pty
```

1. `drover exec <container-id>` → CLI opens a new WebSocket endpoint,
   e.g. `GET /containers/{id}/pty`, sending initial terminal dimensions.
2. Orchestrator calls `POST /containers/{docker_id}/exec`
   (`Tty: true`, `Cmd: ["/var/run/drover/pty"]`) then
   `POST /exec/{exec_id}/start` to attach.
3. Orchestrator spawns two coroutines: WS→Docker (stdin) and
   Docker→WS (PTY output).
4. Resize: CLI detects SIGWINCH and sends a control frame over WS;
   orchestrator calls `POST /exec/{exec_id}/resize?h=N&w=N`.
5. On process exit the orchestrator polls `GET /exec/{exec_id}/json`
   for the exit code, sends it as a closing control frame, and closes
   the WS.

**`/var/run/drover/pty`** is a thin binary (or script) installed by the
drover executor package.  Its default behaviour is to `exec` the
container's shell, but image authors can replace or wrap it to inject
environment setup, logging, banners, or policy checks before handing off
to the shell.  Keeping it at a well-known path lets the orchestrator stay
hardcoded while giving images full composability.

### Component changes

| Component | Changes |
|---|---|
| `DockerClient` | Add `create_exec` / `start_exec` / `resize_exec` |
| Orchestrator routers | New `GET /containers/{id}/pty` WebSocket endpoint |
| Executor package | Install `/var/run/drover/pty` (default: `exec /bin/bash`) |
| CLI `exec.go` | Interactive mode: raw terminal, SIGWINCH handling, bidirectional WS |

## Open Questions

1. **`/var/run/drover/pty` format and install:** Should this be a shell
   script or a compiled binary?  Where does it land — installed by the
   `drover-executor` Python package's post-install, baked into the base
   image, or both?

2. **Missing binary fallback:** If a container image doesn't have
   `/var/run/drover/pty`, should the orchestrator fall back to `/bin/sh`
   (with a logged warning) or return a clear API error so the caller
   knows the image is misconfigured?

3. **WS frame protocol:** PTY output is a raw byte stream; control
   messages (resize ack, exit code) are structured.  Options:
   - Binary WS frames for PTY data + JSON text frames for control.
   - All JSON, with PTY bytes base64-encoded in a `data` field.
   The first avoids encoding overhead; the second keeps the transport
   uniform with the rest of the API.

4. **Exit code propagation:** Docker's exec attach stream closes when the
   process exits, but the exit code is not in-band.  We need to poll
   `GET /exec/{exec_id}/json` after the stream closes.  Is a short
   fixed-delay poll sufficient, or do we need a retry loop?

5. **Session limits:** Should we cap concurrent interactive sessions per
   container?  Uncapped Docker exec instances against a single container
   could be a resource concern.
