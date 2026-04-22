# Drover Orchestrator — Remaining Work

The items below are follow-up work and open design decisions identified during planning that were intentionally deferred.

---

## Command output streaming

The current exec API uses polling (`GET /containers/{id}/exec/{cmd_id}`). Real-time delivery via SSE or WebSocket would reduce latency and load for long-running commands. The choice between SSE (simpler, HTTP-based, one-directional) and WebSocket (bidirectional, more complex) depends on whether callers ever need to send input to a running command.

## Verify GHCR publish workflow (manual)

The `publish.yml` GitHub Actions workflow was written against the original stub Dockerfile. After the Dockerfile was replaced with the production multi-stage build, the workflow needs a manual end-to-end verification to confirm it still builds, tags, and pushes correctly. The `test.yml` workflow now builds the image and smoke-tests the `/health` endpoint on every PR, but `publish.yml` has not yet been validated end-to-end with a real tag push.

## Container log retention

Docker container logs are fetched live from the Docker API and are lost once a container is removed. To preserve diagnostic information after destruction, logs could be captured and stored before the Docker container is removed. It's likely that a homelab setup would have its own logging service, eg Grafana, so this is not a primary or high priority goal. However, if we get streaming for stdout/stderr, the ability to stream overall container logs out for live debugging would certainly be appreciated.

## Container API data model refinement

The Pydantic models in `models.py` cover the initial contract but will likely need adjustment as real usage patterns emerge — e.g. adding pagination to container listings, richer error responses, or additional metadata fields.

---

## Different container auth

I was thinkking about what if we didn't have any auth on the orchestrator at all, but then we offered a web UI as a different (optional) container and have it set up so that the orchestrator and web UI container share a network, and the web UI container exposes a port. The mini containers would not share a port or network with anything else, they are intended to run fully isolated.

The main motivation here is to be able to run the core orchestrator container without any auth for cleanliness. I want a nice UI to monitor things, so I was kind of thinking about combining those two ideas.
