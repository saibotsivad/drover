#!/usr/bin/env bash
# Verify the orchestrator discovers the builder image via Docker labels.
#
# `docker-compose.e2e.yml` builds the builder image with
# `drover.managed=true` / `drover.name=builder` baked into its Dockerfile,
# so GET /images and GET /images/builder should both find it.

# shellcheck source=../lib/common.sh
. "$(dirname "$0")/../lib/common.sh"

echo "[test] 02-images: image discovery"

step_begin "list-images"
api_get "${ORCHESTRATOR_URL}/images"
assert_equals "200" "$E2E_RESPONSE_STATUS" "GET /images status"
NAMES=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.[].name')
assert_contains "$NAMES" "builder" "builder image present in /images"
step_end

step_begin "get-builder-image"
api_get "${ORCHESTRATOR_URL}/images/builder"
assert_equals "200" "$E2E_RESPONSE_STATUS" "GET /images/builder status"
NAME=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.name')
assert_equals "builder" "$NAME" "image name is 'builder'"
MANAGED=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.labels["drover.managed"]')
assert_equals "true" "$MANAGED" "drover.managed label is 'true'"
# The builder ships drover-executor as its CMD, so it must advertise the
# 'exec' capability (comma-separated list in the drover.capabilities label).
CAPABILITIES=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.labels["drover.capabilities"]')
assert_contains "$CAPABILITIES" "exec" "drover.capabilities label declares 'exec'"
step_end

echo "[test] 02-images: ok"
