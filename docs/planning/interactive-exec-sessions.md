# Interactive Exec Sessions

**Status:** Draft / RFC-ish — options laid out, recommendation noted, not yet adopted.

## Goal / Desired Outcome

`drover exec <container-id>` (no `-- <command>`) drops the operator into an
interactive shell inside the micro-container: a real PTY, bidirectional
stdin/stdout, terminal resize handling, and an exit code that mirrors the
shell. `drover exec <id> -- <cmd>` keeps working exactly as it does today.

## Background

Read first: the exec flow (`docs/exec-commands.md`) and the WebSockets ADR
(`docs/decisions/2026-04-11-websockets-for-streaming.md`), which explicitly
chose WebSockets partly to leave the door open for "attach to an interactive
shell" and "send stdin to a running command."

Current state (the constraints that shape every option below):

- **One socket file, mounted as a file.** The orchestrator is the Unix-socket
  *server*; the guest agent dials *out* to `/run/orchestrator.sock`. That path
  is a single-file bind mount (`container_manager.py` ~L391:
  `{host_socket_path}:/run/orchestrator.sock`). The container does **not** see
  the socket *folder* — so "a different filename in the same folder" is not
  visible inside the container without a bind-mount change.
- **Fire-and-forget, no TTY.** `runner.run_command` uses
  `create_subprocess_shell(..., stdin=DEVNULL)`. No PTY, no stdin, no resize.
- **WS is one-way.** `/containers/{id}/ws` only sends server→client today; it
  fans Docker logs + exec output into per-connection queues
  (`connection_manager.py`). There is no client→server path.
- **Exec is capability-gated.** `container_manager._assert_capability` rejects
  with `422` unless the image's `drover.capabilities` label includes the key
  (`docs/capabilities.md`). Interactive will want its own key.
- **CLI already stubs it.** `cli/internal/commands/exec.go` returns
  `interactive_exec_unsupported` when no `--` is present.

## Problem

Interactive PTY traffic is a fundamentally different shape than the existing
command model: long-lived, bidirectional, latency-sensitive, binary-ish, and
1:1 with a client. The current stack (single outbound socket, one-way WS,
no-stdin runner) supports none of these directly. The design question is
*where* to add the bidirectional, per-session plumbing.

There are **three independent axes** to decide. None has a single
obviously-correct answer for our stack, so each lists options.

---

## Axis 1 — Orchestrator ↔ guest transport

How does interactive PTY data get between the orchestrator and the guest
agent inside the container?

**Option 1A — Multiplex over the existing socket.** Add new message types to
the existing newline-delimited JSON protocol: `attach`/`stdin`/`resize`
(orch→guest) and `pty_output`/`pty_exit` (guest→orch), each carrying a
`session_id`. The guest spawns a PTY-backed shell and pumps it through the one
connection it already holds.
- *Pro:* no bind-mount change, no new sockets, reuses the live connection and
  the heartbeat/reconnect machinery already there.
- *Con:* interactive bytes share a connection (and the JSON-line framing) with
  command traffic; PTY output must be base64'd (or similar) to stay
  line-safe; one slow/fat session can head-of-line-block commands.

**Option 1B — Dedicated per-session socket (the original sketch).** The
orchestrator opens a new Unix server at e.g.
`{socket_dir}/{container_id}/{session_id}.sock`, tells the guest over the main
socket to dial it, and the guest runs the PTY session on that fresh
connection.
- *Pro:* clean stream isolation; per-session backpressure; mirrors the "new
  feature in the executor library" framing.
- *Con:* **requires bind-mounting a directory** into the container instead of
  a single file — a real change to the mount convention (and to the
  `/run/orchestrator.sock` path layout), with gVisor `--host-uds=all`
  implications to re-validate. The guest, which today only dials once at
  startup, must now dial sockets on demand.

**Option 1C — Skip the guest; use `docker exec -it`.** The orchestrator holds
the Docker socket already; it could open a native Docker exec with a TTY and
proxy it.
- *Pro:* Docker does the PTY correctly; zero executor changes; zero socket
  changes.
- *Con:* bypasses the "everything flows through the guest agent" architecture
  the whole system is built on; doesn't honor the executor's command model or
  custom-agent overrides; interacts awkwardly with gVisor and with the
  capability model. Listed for completeness; likely rejected on architectural
  consistency grounds.

## Axis 2 — Client ↔ orchestrator transport

**Option 2A — Extend the existing `/containers/{id}/ws`.** Start reading
client→server frames on the same socket and route `stdin`/`resize` to the
session.
- *Pro:* one endpoint; the ADR anticipated exactly this.
- *Con:* that endpoint currently also multiplexes Docker logs for *all*
  watchers; overloading it with a 1:1 interactive session muddies its
  contract.

**Option 2B — New dedicated endpoint, e.g. `/containers/{id}/attach` (or
`/execs/{session_id}/attach`).** A purpose-built bidirectional WS for one
interactive session.
- *Pro:* clean, single-purpose contract; auth/lifecycle/close semantics don't
  have to coexist with the log-fanout endpoint; easier to reason about exit
  codes and resize.
- *Con:* second WS endpoint to maintain.

## Axis 3 — Executor PTY mechanics (new, regardless of the above)

A new capability in `drover-executor`:
- Allocate a PTY (`pty.openpty` / `os.openpty` + `create_subprocess_exec`, or
  `pty.fork`), launch the user's login shell (`$SHELL` or `/bin/sh`).
- Pump shell→socket and socket→shell; apply window size via
  `fcntl.ioctl(fd, termios.TIOCSWINSZ, ...)` on `resize`.
- Report the shell's exit status; clean up the PTY on disconnect/cancel.
- Exposed as an override point on `Agent` (e.g. `on_attach`/`on_interactive`)
  consistent with the existing `on_command` hook.

## Axis 4 — CLI (Go) terminal handling

- Replace the `interactive_exec_unsupported` stub in `exec.go`.
- Put the local terminal into raw mode (`golang.org/x/term`), restore on exit.
- Dial the bidirectional WS, copy stdin→WS and WS→stdout, send `resize` on
  startup and on `SIGWINCH`, exit with the shell's code.
- Refuse / fall back gracefully when stdin is not a TTY (piped input).

---

## Recommendation (for discussion)

**1A + 2B + 3 + 4**, i.e. *multiplex over the existing socket* but expose a
*dedicated client WS endpoint*:

- Avoids the bind-mount/dir change of 1B, which is the single biggest source
  of risk (mount convention + gVisor revalidation) for the least direct
  benefit — head-of-line blocking between commands and an interactive session
  is unlikely to bite a homelab-scale deployment, and can be revisited if it
  does.
- A dedicated client endpoint (2B) keeps the overloaded log-fanout WS contract
  clean and gives interactive sessions their own lifecycle/exit semantics.
- This is the smallest change that honors the existing architecture (all
  traffic flows through the guest agent) and the intent recorded in the
  WebSockets ADR.

If stream isolation later proves necessary, 1B is an additive change behind
the same client-facing contract.

Add a capability key (proposed `interactive`, or `exec.interactive`) gated in
`container_manager` and advertised on executor-bearing images, per
`docs/capabilities.md`'s "Adding a new capability" steps.

## Open Questions

- **Capability granularity:** a distinct `interactive` key, or fold it into
  `exec`? (Leaning distinct — an image may ship a command-only executor.)
- **Session persistence:** are interactive sessions ephemeral-only, or do they
  get a row like `commands` for listing/audit? (Leaning ephemeral — no DB
  persistence, since there's nothing to replay.)
- **Concurrency:** how many simultaneous interactive sessions per container,
  and how do they interact with `--max-concurrent-commands`?
- **Idle/heartbeat:** does an attached session count as activity for the
  idle-timeout reaper, and what closes a session abandoned by a dead client?
- **PTY output framing:** base64 vs. a binary WS frame type to avoid bloating
  interactive latency.
- **Auth on the new endpoint:** reuse the WS Bearer/`?token=` scheme from
  `websockets.py` verbatim.

## Risks and Mitigations

- **Bind-mount change (only if 1B):** re-validate gVisor `--host-uds=all` and
  the file-vs-dir mount; mitigated by recommending 1A.
- **Raw-mode terminal corruption:** always restore terminal state via
  `defer`, including on panic/signal.
- **Leaked PTYs / zombie shells:** kill the process group on
  disconnect/cancel, mirroring `runner.run_command`'s `killpg` on cancel.
- **Capability bypass:** enforce in the orchestrator, not just the CLI/webapp.

## Documentation Impact

- `docs/exec-commands.md` — document the interactive flow and message types.
- `docs/capabilities.md` — add the new capability row.
- `docs/cli.md` — document `drover exec <id>` interactive behaviour.
- `executor/README.md` — document the new PTY hook/override.
- `orchestrator/README.md` — document the new/extended WS endpoint.
- New ADR(s) once adopted: the transport choice (Axis 1) and the capability
  decision are both ADR-worthy.
