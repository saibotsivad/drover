# Drover Orchestrator — Remaining Work

All six implementation phases are complete. The items below are follow-up work and open design decisions identified during planning that were intentionally deferred.

---

## Open Questions

These require design decisions before implementation can begin.

### Command output streaming

The current exec API uses polling (`GET /containers/{id}/exec/{cmd_id}`). Real-time delivery via SSE or WebSocket would reduce latency and load for long-running commands. The choice between SSE (simpler, HTTP-based, one-directional) and WebSocket (bidirectional, more complex) depends on whether callers ever need to send input to a running command.

### ~~Authentication & authorization~~

Resolved — bearer-token authentication is implemented. Set `DROVER_API_KEY` (a SHA-256 hash of the API key) to enable it. See the Authentication section in the README for details. Authorization is currently flat (a single key grants full API access). Scoped permissions (e.g. per-image or per-container) could be added later if needed.

## Follow-Up Items

Concrete work items that can proceed without further design discussion.

### Verify GHCR publish workflow

The `publish.yml` GitHub Actions workflow was written against the original stub Dockerfile. After the Dockerfile was replaced with the production multi-stage build, the workflow needs a manual end-to-end verification to confirm it still builds, tags, and pushes correctly. The `test.yml` workflow now builds the image and smoke-tests the `/health` endpoint on every PR, but `publish.yml` has not yet been validated end-to-end with a real tag push.

### Container log retention

Docker container logs are fetched live from the Docker API and are lost once a container is removed. To preserve diagnostic information after destruction, logs should be captured and stored (in SQLite or on disk) before the Docker container is removed.

### Container API data model refinement

The Pydantic models in `models.py` cover the initial contract but will likely need adjustment as real usage patterns emerge — e.g. adding pagination to container listings, richer error responses, or additional metadata fields.
