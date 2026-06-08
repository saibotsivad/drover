# Lifecycle Management

Drover's lifecycle management is spread across several layers, and a single
event (a restart, a crash, an idle timeout) ripples through all of them at once.
This document is the one place that describes, per layer, what *should* happen at
each lifecycle event and what *actually* happens today, so the team can see the
deltas in one view and decide what to close.

> **Status:** Working draft for team review. The intent here is consolidated
> from `docs/interactive-sessions.md`, `docs/exec-commands.md`, the orchestrator
> code, and recent design discussion. Once we agree on the intended behaviour,
> the follow-up is a doc/comment sweep to bring every other document and the
> code in line with what we settle here.

## Reading convention

Each behaviour is tagged so intent and reality never get conflated:

- **Intent** — how we want the system to behave.
- **Today** — what the code actually does as of this writing.
- **Gap** — the delta between the two that the team needs to close (or
  consciously accept).

When **Intent** and **Today** match, only **Today** is shown.

---

## The layers

Lifecycle state lives in, and is acted on by, these layers:

1. **Host machine + Docker daemon** — the substrate. If either dies, everything
   above it dies with it. Drover does not manage this layer; it only reacts to
   it on restart.
2. **Drover orchestrator** — itself a container. The FastAPI app that owns all
   state transitions, the Unix-socket *server* for every micro-container, and
   the reconciler/reaper. When "Drover restarts," this is what restarts.
3. **SQLite database** — the **state of record**. It survives orchestrator
   restarts (WAL file on a mounted volume) and is the thing reconciliation reads
   to rediscover the world. Every lifecycle transition is a write here.
4. **Micro-containers** — the launched mini-containers, each running a **guest
   agent** that dials *out* to the orchestrator's per-container control socket.
   They run independently of the orchestrator process and do **not** die when
   the orchestrator restarts (only when the host/Docker dies, or the
   orchestrator explicitly stops them).
5. **Exec commands** — fire-and-forget command invocations against a
   micro-container, persisted in `commands` / `command_messages`.
6. **Interactive sessions** — long-lived PTY sessions, persisted in `sessions`
   (specified in `docs/interactive-sessions.md`; not yet built).

The recurring theme: **the micro-container, its guest agent, its shells, its
PTYs, and any in-flight command all live below the orchestrator and outlive an
orchestrator restart.** The only things an orchestrator crash destroys are the
orchestrator's *connections* to them (the listening sockets and their accepted
connections). Almost every lifecycle question below is really "how do we
re-establish, or cleanly give up on, those connections after a gap?"

---

## Cross-cutting concepts

### State of record and reconciliation

The DB is authoritative across restarts; the live socket connections and
in-memory task state are not. On every start the orchestrator runs a
**reconciliation pass** (`container_manager.sync_containers()`) that walks the
non-terminal DB rows and reconciles them against Docker and the socket files.
Everything in this document's "orchestrator start" section is part of, or should
become part of, that pass.

### The control plane and liveness signals

Each micro-container has one **control-plane** Unix socket
(`…/{container_id}/orchestrator.sock`); the guest dials in and keeps a
persistent connection. Over it the guest sends:

- `ready` — sent once after (re)connecting; drives `initializing`/`resuming` →
  `running`.
- `heartbeat` — sent on the guest's own schedule; updates `last_seen`.
- `output` / `result` / `done` — exec-command traffic.

**Today** liveness is *one-way and passive*: the guest pushes `heartbeat`, the
orchestrator records `last_seen`, and the reaper acts on staleness. There is no
orchestrator-initiated request the guest must answer.

**Intent** (see [Liveness and the restart handshake](#liveness-and-the-restart-handshake))
we want an orchestrator-initiated **ping/pong** so that, after a restart, the
orchestrator can *actively* confirm a guest is still alive on a reconnected
socket rather than waiting passively for the next heartbeat.

### The reaper

A background loop stops any `running` container whose `last_seen` is older than
its `timeout_seconds`. It is the mechanism that reclaims idle micro-containers.
Interactive sessions interact with it (a container with a live session must not
be reaped — see the sessions section).

---

## Lifecycle events

### Host or Docker daemon killed while Drover is running

Killing the host or the Docker daemon tears down the entire Drover stack: the
orchestrator container and every micro-container stop at once.

- **Orchestrator:** on host/daemon recovery it goes through its normal **start
  sequence** (below). It does not assume anything about prior in-memory state —
  the DB is the only thing that survived.
- **Micro-containers:** **not auto-restarted.** Drover does not set a Docker
  restart policy on them, and the orchestrator does not recreate them. On
  restart the reconciler will find their Docker containers `exited` (or gone)
  and mark them accordingly.
- **Exec commands / sessions:** their micro-containers are gone, so they are
  terminal by definition; reconciliation marks them.

**Gap (naming):** when reconciliation finds a micro-container that exited while
the orchestrator was down, the row should carry a clear, specific terminal
reason (e.g. `exited_while_orchestrator_offline`) rather than a generic status,
so an operator can tell "this exited on its own" from "this exited *because* the
orchestrator was offline." See the [error-code registry](#error-code-registry-proposed).

### Orchestrator graceful stop

On a clean shutdown the orchestrator cancels in-flight init/resume tasks and
closes (does **not** destroy) its sockets — socket *files* are preserved.
Micro-containers keep running. The next start reconciles them. There is no
explicit "draining" of sessions or commands today.

### Orchestrator crash / unexpected restart

Identical inputs to a graceful stop from the micro-containers' perspective — the
guests see their control connections drop — but the orchestrator did not get to
run shutdown logic, so in-flight task state is simply lost. Recovery is entirely
the job of the start sequence. The interesting cases (in-flight commands,
running sessions) are detailed in their own sections below; the short version is
that the micro-containers and their work survive, and the question is how
aggressively the orchestrator re-attaches versus gives up.

### Orchestrator start sequence (reconciliation)

When the orchestrator starts it reconciles DB state against the discoverable
world **before** it begins serving the reaper and accepting new work. The
intended full sequence:

#### 1. Containers

For each container row not already terminal (`destroyed` / `error`):

- **Today**
  - Rows in `initializing` / `resuming` are assumed to have been interrupted
    mid-transition and are forced to `error` with code `orchestrator_crash`
    (their Docker container, if any, is force-removed).
  - For every other row, inspect Docker:
    - Docker container missing ⇒ mark `destroyed`.
    - Docker status disagrees with the DB (and we're not mid-transition) ⇒ adopt
      the Docker-derived status.
    - Still `running` ⇒ **re-listen on the control socket** (`create_socket`)
      and resume log capture.
- **Intent (adds active liveness):** for a row that the DB and Docker agree is
  `running` *and* whose control socket file exists, re-listening is not enough —
  the guest may be wedged, or the container may be a zombie. After re-listening,
  the orchestrator should **ping** the guest and wait for a **pong**:
  - pong within the timeout ⇒ confirmed alive, leave `running`.
  - no pong within the reaper timeout (or no socket file, or the guest never
    re-dials) ⇒ stop/remove the container via Docker and mark it terminal with a
    specific reason such as `unreachable_after_orchestrator_restart`.
  - Docker reports it is **not** running (exited while we were down) ⇒ mark
    terminal with `exited_while_orchestrator_offline`.
- **Gap:** the ping/pong handshake and the two specific reasons above don't
  exist yet. Today a wedged-but-`running` container is simply re-listened and
  left alone, and a container that exited while the orchestrator was offline is
  marked with the generic Docker-derived `stopped`, losing the "while we were
  offline" nuance.

#### 2. Exec commands

- **Today:** **not reconciled.** A command left `pending` or `running` when the
  orchestrator crashed stays that way forever. Any output the guest produced
  during the gap is lost (it was written to a dropped socket), and there is no
  catch-up.
- **Intent (for discussion):** at minimum, reconcile orphaned in-flight
  commands to a terminal state with a specific reason (e.g.
  `orchestrator_offline`) so they don't appear perpetually running. Whether to
  attempt *recovery* of a command still running in the guest (re-attach and keep
  streaming) is an open question — it is lower value than session recovery
  because exec is already a poll/replay model, but the same control socket is
  available to do it.
- **Gap:** command reconciliation is entirely unimplemented.

#### 3. Interactive sessions

- **Today:** the `sessions` table and all session machinery are unbuilt, so
  there is nothing to reconcile yet. This section is the *intended* behaviour to
  build against.
- **Intent:** mirror the container logic, because a session's shell/PTY/emulator
  live in the (still-running) micro-container and survive an orchestrator
  restart just like the container does. For each non-terminal session row:
  - container is no longer active ⇒ the session cannot survive; mark it terminal
    (`container_stopped`, or `exited_while_orchestrator_offline` to match the
    container's own reason).
  - container is active and the session's data-plane socket file exists ⇒
    **re-listen on the session socket and attempt to re-establish** the session
    rather than killing it (see [Interactive session lifecycle](#interactive-session-lifecycle)
    for why this is viable). If the guest does not re-dial / does not respond
    within the timeout, give up and mark it terminal
    (`unreachable_after_orchestrator_restart`).
- **Gap:** everything here is design, not code. Crucially, the *previous* plan
  had reconciliation simply marking every non-`closed` session `closed` on
  restart (treating a crash as a session death). We are now leaning toward
  **recovery over kill** — see the dedicated section.

### Micro-container lifecycle

States (`containers.status`): `initializing → running → stopping → stopped →
resuming → destroying → destroyed`, plus `error` (carrying an `error_code`).

- **create:** row inserted `initializing`; background task creates the control
  socket, then the Docker container; transitions to `running` only when the
  guest sends `ready`. A watchdog fails it to `error` if `ready` doesn't arrive
  within `init_timeout_seconds`.
- **running:** guest connected; `heartbeat` keeps `last_seen` fresh; reaper
  watches for idle timeout.
- **stop:** control socket *closed but file preserved*; Docker container
  stopped; row → `stopped`. (Resume relies on the preserved socket file +
  folder.) Per the sessions spec, **no session survives a stop.**
- **resume:** row → `resuming`; background task re-creates the socket and starts
  Docker; → `running` on `ready`; watchdog → `error` (`resume_timeout`) on
  failure.
- **destroy:** row → `destroying`; Docker removed; socket **destroyed** (file +
  per-container folder removed, including the `sessions/` subtree); row →
  `destroyed`.
- **error:** terminal; `error_code` records why (init/resume timeout, docker
  error, `orchestrator_crash`, …).

### Exec command lifecycle

States (`commands.status`): `pending → running → complete`.

- **pending:** orchestrator generated the `command_id`, inserted the row, and
  sent the command over the control socket.
- **running:** first `output` message arrived.
- **complete:** `result` (exit code) arrived; `exit_code` set.

Commands are fire-and-forget and persisted as they stream, so multiple can be in
flight per container. **Today** there is no reconciliation for commands across
an orchestrator restart (see start sequence, step 2) — this is the main lifecycle
gap on the exec side.

### Interactive session lifecycle

The full transport/lifecycle is specified in
[`docs/interactive-sessions.md`](interactive-sessions.md); this section covers
only how sessions behave across **orchestrator restart**, which that spec
currently punts on ("a session socket left behind by an orchestrator crash is
ignored; removed when the container is destroyed").

States (`sessions.status`, per spec): `starting → running → closed`. Pause/resume
is *not* a session state — a session is "running" whether or not a client is
attached and whether or not the PTY is paused.

#### Why a crash is not a session death

The expensive, stateful parts of a session — the shell, the PTY, and the `pyte`
emulator screen model — all live **inside the micro-container**, which keeps
running across an orchestrator restart. A crash destroys only:

- the orchestrator's listening end of the per-session data-plane socket, and
- the WebSocket to whatever client was attached (if any).

Both are recoverable, and three existing design properties make recovery clean:

1. **Socket files persist.** Session sockets are host files the orchestrator
   created; nothing in Docker removes them. Re-listening on a stale session
   socket is the same move `create_socket` already makes for the control socket.
2. **Snapshot-on-resume makes the data plane re-syncable.** The guest keeps the
   emulator current at all times and sends a *full fresh snapshot* on resume, so
   bytes lost in the crash gap are irrelevant — the screen model is
   authoritative and re-synced on reconnect.
3. **"Detached/paused" is already a first-class state.** After a restart no
   client is attached to anything, which is exactly the already-designed
   "session running, no client, PTY paused" case. Crash recovery collapses into
   it; the only new mechanic is re-listen + guest re-dial, then a normal resume
   when a client reconnects.

#### Intended restart behaviour

- **Intent:** treat a restart as a *reconnect*, not a death. The orchestrator
  re-listens on each `running` session's data socket; the guest, on noticing its
  data-plane connection dropped, **self-pauses** (keeps the shell and emulator
  alive, keeps feeding the emulator) and re-dials with backoff; when a client
  reconnects via WebSocket, the orchestrator drives a normal `session_pty_resume`
  and the operator is back where they were. A session is only marked terminal if
  its container is gone, or if the guest never re-establishes within the timeout.
- **Today:** unbuilt. The current spec ignores the leftover socket and (in the
  prior implementation plan) would have marked the session `closed` on restart.
- **Gaps to close (these are the real design items):**
  - **Guest self-pause on data-plane drop + re-dial.** Pause is currently only
    *orchestrator-driven* (`session_pty_pause`). We need to define guest
    behaviour for an *unsolicited* data-plane disconnect: detect EOF/EPIPE →
    self-pause → retry dial. No application-level acknowledgement protocol is
    needed — connection-liveness detection suffices, because the snapshot model
    already makes the stream idempotent.
  - **Orchestrator re-listen from DB.** `sync_containers` must re-listen on the
    session sockets of `running` sessions, not just the control socket.
  - **Distinguishing a truly dead session.** If the shell exited (or the guest
    itself restarted) during the gap, the session really is gone. The clean way
    to tell live from dead is a lightweight **guest session inventory** on
    control-plane reconnect (the guest announces which `session_id`s it still
    holds; the orchestrator marks any `running` row the guest doesn't claim
    terminal). This is strictly better than blanket-killing every session, and
    is a candidate to scope as follow-up work.

#### Sessions and the reaper

A container with any non-`closed` session must be **exempt from the idle
reaper** — an unattached-but-running session (the "start a long job, detach, let
it run" case) is precisely what we must not reap. This exemption is only correct
if reconciliation keeps session rows honest: a row that says `running` must
correspond to a session that is genuinely recoverable (or be promptly marked
terminal if not). The recovery model above is what keeps the exemption from
turning a crash into immortal containers.

---

## Liveness and the restart handshake

**Today:** liveness is the one-way `heartbeat` → `last_seen` → reaper chain. It
answers "has this guest gone quiet for too long?" but not "is this specific
reconnected guest responding *right now*?"

**Intent:** add an orchestrator-initiated **ping/pong** on the control plane:

- The orchestrator can send `{"type":"ping"}` (or similar); the guest replies
  `{"type":"pong"}`. Both sides support it.
- Primary use is the **restart handshake**: after re-listening on a `running`
  container's socket, the orchestrator pings and waits up to the reaper timeout
  for a pong before trusting the row. No pong ⇒ stop + mark
  `unreachable_after_orchestrator_restart`.

**Open question:** whether a dedicated ping/pong is worth it versus simply
waiting one reaper interval for the next passive `heartbeat` after re-listening.
A dedicated ping gives a faster, more deterministic answer and a cleaner failure
reason; relying on the existing heartbeat is zero new protocol. Team to decide.

---

## Error-code registry (proposed)

Lifecycle reconciliation needs specific, greppable terminal reasons. Proposed
additions (names open for bikeshedding), to be applied consistently across
`containers.error_code`, command status, and `sessions.exit_status`:

| Reason | Applies to | Meaning |
|---|---|---|
| `orchestrator_crash` | containers (existing) | Row was mid-transition (`initializing`/`resuming`) when the orchestrator restarted. |
| `exited_while_orchestrator_offline` | containers, sessions | Reconciliation found the Docker container had exited while the orchestrator was down. |
| `unreachable_after_orchestrator_restart` | containers, sessions | Re-listened after restart but the guest never re-established / failed the ping within the timeout; force-stopped. |
| `orchestrator_offline` | commands | In-flight command orphaned by an orchestrator restart; no recovery attempted. |
| `container_stopped` | sessions (existing in spec) | Session swept because its container stopped/destroyed (no session survives a stop). |

---

## Open questions for the team

1. **Session recovery vs. kill on restart** — confirm we want recovery
   (re-listen + guest re-dial + resume) as the target, with kill only as the
   fallback when the container is gone or the guest doesn't re-establish.
2. **Ping/pong vs. passive heartbeat** for the restart liveness check (above).
3. **Exec-command reconciliation** — mark-orphaned-terminal only, or also attempt
   re-attach to a still-running command?
4. **Micro-container race recovery** — recovering a guest that was mid-dial or
   mid-handshake when the orchestrator crashed may have edge cases worth a
   dedicated, separately-scoped hardening pass rather than blocking this work.
5. **Final naming** of the error/exit reasons in the registry above.

Once these are settled, the follow-up is a sweep of `docs/interactive-sessions.md`,
`docs/exec-commands.md`, the orchestrator `README.md`, and the relevant code
comments to match.
