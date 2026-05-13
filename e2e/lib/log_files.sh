#!/usr/bin/env bash
# Helpers for asserting on captured-log REST endpoints.
#
# The endpoints are documented in docs/observability.md.  These wrappers
# exist so individual tests stay short:
#
#   assert_log_files_contains "$CONTAINER_ID" "0.log"
#   assert_log_file_contains  "$CONTAINER_ID" "0.log" "expected substring"
#   assert_log_files_empty    "$CONTAINER_ID"
#   assert_log_file_missing   "$CONTAINER_ID" "0.log"
#
# Each helper performs a single GET, fails the test on a non-200 status
# (where applicable), and uses jq to extract / verify the body.

assert_log_files_contains() {
	local cid="$1" name="$2" msg="${3:-/logs/files contains $name}"
	api_get "${ORCHESTRATOR_URL}/containers/${cid}/logs/files"
	assert_equals "200" "$E2E_RESPONSE_STATUS" "/logs/files HTTP status"
	local present
	present=$(printf '%s' "$E2E_RESPONSE_BODY" | jq --arg n "$name" 'index($n) != null' 2>/dev/null || echo "false")
	if [ "$present" != "true" ]; then
		e2e_fail "$msg (body=$E2E_RESPONSE_BODY)"
	fi
	e2e_pass "$msg"
}

assert_log_files_empty() {
	local cid="$1" msg="${2:-/logs/files is empty}"
	api_get "${ORCHESTRATOR_URL}/containers/${cid}/logs/files"
	assert_equals "200" "$E2E_RESPONSE_STATUS" "/logs/files HTTP status"
	local len
	len=$(printf '%s' "$E2E_RESPONSE_BODY" | jq 'length' 2>/dev/null || echo "?")
	if [ "$len" != "0" ]; then
		e2e_fail "$msg (length=$len body=$E2E_RESPONSE_BODY)"
	fi
	e2e_pass "$msg"
}

assert_log_file_contains() {
	local cid="$1" name="$2" needle="$3" msg="${4:-/logs/files/$name contains '$needle'}"
	api_get "${ORCHESTRATOR_URL}/containers/${cid}/logs/files/${name}"
	assert_equals "200" "$E2E_RESPONSE_STATUS" "/logs/files/$name HTTP status"
	if [[ "$E2E_RESPONSE_BODY" != *"$needle"* ]]; then
		e2e_fail "$msg (body=$E2E_RESPONSE_BODY)"
	fi
	e2e_pass "$msg"
}

assert_log_file_missing() {
	local cid="$1" name="$2" msg="${3:-/logs/files/$name returns 404}"
	api_get "${ORCHESTRATOR_URL}/containers/${cid}/logs/files/${name}"
	assert_equals "404" "$E2E_RESPONSE_STATUS" "$msg"
}
