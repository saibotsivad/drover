# ADR: Explicit ready message for container initialization

**Date:** 2026-04-21
**Status:** Accepted

## Context

Container initialization is being made asynchronous: `POST /containers` now
returns immediately with status `initializing`, and the container transitions
to `running` once it is ready to accept exec commands.

The question is what event should trigger the `initializing` → `running`
transition. Two implicit options were considered before settling on the
approach documented here:

- **TCP connection arrival**: the orchestrator already detects when the guest
  agent connects to the Unix socket. This is a concrete event but it only
  signals that the agent process started, not that the container is ready to
  do useful work.

- **First heartbeat**: the agent sends periodic heartbeats once connected.
  The first heartbeat could be treated as a readiness signal, but heartbeats
  are a liveness mechanism and overloading them with a readiness meaning would
  blur that distinction.

Neither option accounts for containers that need to start background services,
wait for a database, load models, or do any other work before they can
meaningfully handle commands.

## Decision

We add an explicit `ready` message to the guest-to-orchestrator protocol:

```json
{"type": "ready"}
```

The orchestrator transitions the container from `initializing` to `running`
only upon receipt of this message.

The `Agent` base class in the executor sends `ready` automatically after
`on_connect()` returns. Subclasses perform their startup work inside
`on_connect()`; no manual call to `send_ready()` is required.

## Reasoning

### Explicit beats implicit

A dedicated message makes the contract unambiguous: the container declares
itself ready, rather than the orchestrator inferring readiness from a side
effect. This is easier to reason about, easier to test, and easier to explain
to contributors.

### on_connect() is the right hook

The `Agent` class already has an `on_connect()` override point designed for
startup customization. Tying `ready` emission to the return of `on_connect()`
means container authors have a single, clearly-named place to put startup
logic, with no extra API to learn. The behavior is: "do your startup, return,
and the framework handles the rest."

### Heartbeats retain a single responsibility

Heartbeats continue to mean "the agent is alive", not "the agent is ready".
This keeps both signals easy to interpret independently.

## Consequences

- The executor's `protocol.py` gains an `encode_ready()` function.
- `Agent.run()` sends `ready` after `on_connect()` completes.
- `SocketManager` gains a `_handle_ready()` handler that issues:
  `UPDATE containers SET status = 'running' WHERE id = ? AND status = 'initializing'`
- Containers that never send `ready` (e.g. due to an exception in
  `on_connect()`, or a crash before connecting) will remain in `initializing`
  until the orchestrator's init timeout fires and transitions them to `error`.
- The `socket_manager.py` module-level docstring and `protocol.py` header
  comment must be updated to include `ready` in the message type lists.
