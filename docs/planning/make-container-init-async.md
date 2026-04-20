# Plan: Async Container Initialization

## Background

Currently `POST /containers` blocks until Docker has created and started the
container, then returns 201 with the container in `running` status. This means
the caller waits for the full Docker round-trip before getting a response.

The goal is to return immediately with a new `initializing` status and let the
Docker work happen in a background task, so callers can proceed to poll
`GET /containers/{id}` for the transition to `running`.

## Status Enum Changes

Two new values are added to `ContainerStatus` (`models.py`):

| Status | Meaning |
|---|---|
| `initializing` | Container row created; Docker create/start in progress |
| `error` | A non-recoverable error occurred; orchestrator has cleaned up |

The DB schema default for the `status` column (`database.py`) changes from
`'running'` to `'initializing'` for clarity, even though explicit values are
always passed on insert.

Full status lifecycle:

```
initializing → running → stopping → stopped → resuming → running → ...
                                  ↘ destroyed
initializing → error
running      → error  (unexpected Docker failure)
```

## Code Changes

### 1. `models.py` — `ContainerStatus` enum

Add `initializing` and `error` values.

### 2. `database.py` — schema default

Change the `status` column default from `'running'` to `'initializing'`.

### 3. `container_manager.py` — `create_container`

Split the existing method into two phases:

**Phase 1 (synchronous, before returning):**
1. Validate image / privileged config (unchanged).
2. Generate container ID and create the Unix socket (unchanged — must exist
   before the container starts so the bind-mount target is a file).
3. Insert the DB row with `status = 'initializing'` and `docker_id = NULL`.
4. Return the `ContainerResponse` immediately (HTTP 201).

**Phase 2 (background task via `asyncio.create_task`):**
1. Call `docker.create_container(...)` to get a `docker_id`.
2. Update the DB row with the `docker_id`.
3. Call `docker.start_container(docker_id)`.
4. On success: update DB status to `'running'`.
5. On any failure: update DB status to `'error'`, destroy the socket, and
   force-remove the Docker container if one was created — same cleanup logic
   as today, just moved into the background task.

### 4. `container_manager.py` — `sync_containers` (startup reconciliation)

`sync_containers` runs at startup to reconcile DB state against Docker.
Currently it skips rows in mid-transition statuses (`stopping`, `resuming`,
`destroying`). `initializing` must be added to that list so a crash mid-init
doesn't get incorrectly reconciled against a Docker container that may not
exist yet.

Additionally: if a row is still `initializing` after the reconciliation pass
(i.e. Docker has no matching container), it should be transitioned to `error`
rather than left stuck.

## What Does Not Change

- `GET /containers/{id}` — already reads directly from the DB row; no changes
  needed.
- `GET /containers/{id}/exec/{command_id}` and `POST /containers/{id}/exec` —
  unaffected.
- Socket creation timing — the socket is still created synchronously before
  the DB insert, so the bind-mount path always exists when Docker needs it.
- Error cleanup logic — same socket destruction and force-remove steps, just
  relocated into the background task.

## Open Questions

1. **Should `POST /containers/{id}/exec` be rejected while status is
   `initializing`?** Currently exec commands are only dispatched over the Unix
   socket once the guest agent connects, so a command submitted during
   `initializing` would just sit as `pending`. This is arguably fine, but we
   could also return a 409 to make the contract explicit.

2. **Should there be a timeout on the background init task?** If Docker hangs,
   the container stays `initializing` forever. A configurable deadline (e.g.
   same `timeout_seconds` as the container itself) that transitions to `error`
   on expiry would bound the stuck-state window.

3. **Error detail visibility.** The `error` status tells the caller something
   went wrong, but not what. Should `ContainerResponse` include an optional
   `error_message` field, or is the status alone sufficient for now?
