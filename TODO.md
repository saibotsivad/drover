# Drover Orchestrator — Remaining Work

The items below are follow-up work and open design decisions identified during planning that were intentionally deferred.

---

## List-endpoint pagination

Container log retention landed via `DROVER_ENABLE_CONTAINER_LOGS` and the
`/containers/{id}/logs/files` endpoints (see `docs/observability.md`),
but `GET /containers/{id}/logs` is still the unmodified live-Docker
proxy and the rest of the list endpoints (`GET /containers`,
`GET /images`, exec messages) have no pagination contract. The next
plan should design a single `since` / `until` / `limit` / `offset`
shape and retrofit it everywhere — including broadening
`/containers/{id}/logs` to read from disk when retention is enabled.
This is the prerequisite for cutting a release that publicly advertises
log retention as a feature.

## Container API data model refinement

The Pydantic models in `models.py` cover the initial contract but will need adjustment to add pagination to container listings, richer error responses, and possibly additional metadata fields.
