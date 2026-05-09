# Drover Orchestrator — Remaining Work

The items below are follow-up work and open design decisions identified during planning that were intentionally deferred.

---

## Command output streaming

The current exec API uses polling (`GET /containers/{id}/exec/{cmd_id}`). Real-time delivery via SSE or WebSocket would reduce latency and load for long-running commands. There's an RFC open that details some stuff about that with WebSockets.

## Container log retention

Docker container logs are fetched live from the Docker API and are lost once a container is removed. To preserve diagnostic information after destruction, logs could be captured and stored before the Docker container is removed. It's likely that a homelab setup would have its own logging service, eg Grafana, so this is not a primary or high priority goal. However, if we get streaming for stdout/stderr, the ability to stream overall container logs out for live debugging would certainly be appreciated.

## Container API data model refinement

The Pydantic models in `models.py` cover the initial contract but will need adjustment to add pagination to container listings, richer error responses, and possibly additional metadata fields.
