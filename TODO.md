# Drover Orchestrator — Remaining Work

All six implementation phases are complete. The items below are follow-up work and open design decisions identified during planning that were intentionally deferred.

---

## Open Questions

These require design decisions before implementation can begin.

### Command output streaming

The current exec API uses polling (`GET /containers/{id}/exec/{cmd_id}`). Real-time delivery via SSE or WebSocket would reduce latency and load for long-running commands. The choice between SSE (simpler, HTTP-based, one-directional) and WebSocket (bidirectional, more complex) depends on whether callers ever need to send input to a running command.

### Authentication & authorization

The REST API has no auth layer. Before any external exposure, we need to decide on an auth scheme — API keys, mTLS, OAuth tokens, or something else — and whether authorization is flat or scoped (e.g. per-image or per-container permissions).

### Container "ready to delete" signal

Containers currently rely on the idle timeout to get reaped. A new message type (e.g. `{"type": "done"}`) from the guest agent would let short-lived containers signal completion immediately, avoiding unnecessary timeout waits and enabling tighter orchestration workflows.

---

## Follow-Up Items

Concrete work items that can proceed without further design discussion.

### Test suite

No tests exist yet. Needs unit tests for ID generation, database operations, socket protocol parsing, and container state machine transitions. Integration tests should exercise the full lifecycle against a real Docker socket.

### Verify GHCR publish workflow

The `publish.yml` GitHub Actions workflow was written against the original stub Dockerfile. After the Dockerfile was replaced with the production multi-stage build, the workflow needs a manual end-to-end verification to confirm it still builds, tags, and pushes correctly.

### Orchestrator restart reconciliation

If the orchestrator crashes or restarts, SQLite may contain `running` containers that are actually stopped or gone in Docker. The per-request sync in `get_container()` handles this lazily, but a startup sweep should proactively reconcile all non-terminal containers against Docker state and re-establish socket listeners for any that are still alive.

### Container log retention

Docker container logs are fetched live from the Docker API and are lost once a container is removed. To preserve diagnostic information after destruction, logs should be captured and stored (in SQLite or on disk) before the Docker container is removed.

### Input validation hardening

Validation is minimal — image names, labels, and environment variables are accepted as bare strings with no format checks. Needs constraints on image name patterns (e.g. alphanumeric, hyphens, slashes only), timeout upper bounds, label length limits, and environment variable key validation to prevent injection or misuse.

### Container API data model refinement

The Pydantic models in `models.py` cover the initial contract but will likely need adjustment as real usage patterns emerge — e.g. adding pagination to container listings, richer error responses, or additional metadata fields.
