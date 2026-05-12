#!/usr/bin/env bash
# Verify both running services answer health checks.
#
# Orchestrator: GET /health -> 200, {"healthy": true, ...}
# Webapp:       GET /health -> 200, {"healthy": true}
#
# The builder image is a template (not a long-running service) so it has
# no health endpoint — its presence is exercised indirectly by test 03.

# shellcheck source=../lib/common.sh
. "$(dirname "$0")/../lib/common.sh"

echo "[test] 01-health: service health checks"

step_begin "orchestrator-health"
api_get "${ORCHESTRATOR_URL}/health"
assert_equals "200" "$E2E_RESPONSE_STATUS" "orchestrator /health status"
HEALTHY=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.healthy')
assert_equals "true" "$HEALTHY" "orchestrator reports healthy"
step_end

step_begin "webapp-health"
api_get "${WEBAPP_URL}/health"
assert_equals "200" "$E2E_RESPONSE_STATUS" "webapp /health status"
HEALTHY=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.healthy')
assert_equals "true" "$HEALTHY" "webapp reports healthy"
step_end

echo "[test] 01-health: ok"
