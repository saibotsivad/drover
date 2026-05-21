#!/usr/bin/env bash
# Stop then resume a privileged container, asserting the container is
# reachable again afterwards via the same `echo $DROVER_TEST_VAR` exec
# that test 03 uses. This guards the `stopped --> resuming --> running`
# transition end-to-end: the socket file is recreated, Docker restarts
# the container, the guest agent reconnects, and exec succeeds.
#
# After resume the orchestrator transitions to `running` as soon as
# Docker confirms start, but the guest agent hasn't necessarily
# reconnected to /run/orchestrator.sock yet. We retry the exec POST
# while it returns 409 ContainerNotConnected so the test exercises the
# real reconnect window rather than masking it with a fixed sleep.
#
# Uses the privileged path so the test runs on any host with Docker —
# matching test 03 and avoiding the gVisor requirement of test 04.

# shellcheck source=../lib/common.sh
. "$(dirname "$0")/../lib/common.sh"

echo "[test] 06-stop-resume: stop then resume preserves reachability"

# Retry POST /containers/{id}/execs while the orchestrator reports the
# container is not yet connected (409). Returns 0 with the command_id in
# $E2E_RESPONSE_BODY when the exec was accepted; returns 1 on timeout.
post_exec_with_retry() {
	local container_id="$1" command="$2" timeout="${3:-30}"
	local body
	body=$(jq -nc --arg c "$command" '{command: $c}')
	local i=0
	while [ "$i" -lt "$timeout" ]; do
		api_post "${ORCHESTRATOR_URL}/containers/${container_id}/execs" "$body"
		if [ "$E2E_RESPONSE_STATUS" = "201" ]; then
			return 0
		fi
		if [ "$E2E_RESPONSE_STATUS" != "409" ]; then
			return 1
		fi
		i=$((i + 1))
		sleep 1
	done
	return 1
}

# --- 1. create -------------------------------------------------------------

step_begin "create-container"
step_set_wait "running" 30
REQUEST_BODY='{"image": "builder", "privileged": true, "env": {"DROVER_TEST_VAR": "hello_drover"}}'
api_post "${ORCHESTRATOR_URL}/containers" "$REQUEST_BODY"
assert_equals "201" "$E2E_RESPONSE_STATUS" "POST /containers status"
CONTAINER_ID=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.id')
assert_not_empty "$CONTAINER_ID" "container id returned"
echo "  container_id=$CONTAINER_ID"
wait_container_status "$CONTAINER_ID" "running" 30 \
	|| e2e_fail "container did not reach running"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "running" "$FINAL" "final status is running"
step_end

# --- 2. exec (pre-stop) ----------------------------------------------------

step_begin "exec-before-stop"
step_set_wait "complete" 30
EXEC_BODY='{"command": "echo $DROVER_TEST_VAR"}'
api_post "${ORCHESTRATOR_URL}/containers/${CONTAINER_ID}/execs" "$EXEC_BODY"
assert_equals "201" "$E2E_RESPONSE_STATUS" "POST exec status"
COMMAND_ID=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.command_id')
assert_not_empty "$COMMAND_ID" "command id returned"

EXEC_RESULT=$(wait_exec_complete "$CONTAINER_ID" "$COMMAND_ID" 30) \
	|| e2e_fail "exec did not complete"
EXIT_CODE=$(printf '%s' "$EXEC_RESULT" | jq -r '.exit_code')
STDOUT=$(printf '%s' "$EXEC_RESULT" | jq -r '[.messages[] | select(.stream == "stdout") | .data] | join("")')
STDERR=$(printf '%s' "$EXEC_RESULT" | jq -r '[.messages[] | select(.stream == "stderr") | .data] | join("")')
step_set_exec_result "$EXIT_CODE" "$STDOUT" "$STDERR"
assert_zero "$EXIT_CODE" "exec exit code"
assert_contains "$STDOUT" "hello_drover" "exec stdout"
step_end

# --- 3. stop ---------------------------------------------------------------

step_begin "stop-container"
step_set_wait "stopped" 30
api_post "${ORCHESTRATOR_URL}/containers/${CONTAINER_ID}/stop"
if [ "$E2E_RESPONSE_STATUS" != "200" ] && [ "$E2E_RESPONSE_STATUS" != "202" ]; then
	e2e_fail "POST /containers/{id}/stop returned $E2E_RESPONSE_STATUS"
fi
wait_container_status "$CONTAINER_ID" "stopped" 30 \
	|| e2e_fail "container did not reach stopped"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "stopped" "$FINAL" "final status is stopped"
step_end

# --- 4. resume -------------------------------------------------------------

step_begin "resume-container"
step_set_wait "running" 30
api_post "${ORCHESTRATOR_URL}/containers/${CONTAINER_ID}/resume"
if [ "$E2E_RESPONSE_STATUS" != "200" ] && [ "$E2E_RESPONSE_STATUS" != "202" ]; then
	e2e_fail "POST /containers/{id}/resume returned $E2E_RESPONSE_STATUS"
fi
wait_container_status "$CONTAINER_ID" "running" 30 \
	|| e2e_fail "container did not reach running after resume"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "running" "$FINAL" "final status is running after resume"
step_end

# --- 5. exec (post-resume) -------------------------------------------------
# The container's status is `running` but the guest agent may still be
# reconnecting to /run/orchestrator.sock. Retry the exec POST while the
# orchestrator returns 409 ContainerNotConnected.

step_begin "exec-after-resume"
step_set_wait "complete" 30
post_exec_with_retry "$CONTAINER_ID" 'echo $DROVER_TEST_VAR' 30 \
	|| e2e_fail "exec POST never accepted after resume (last status=$E2E_RESPONSE_STATUS body=$E2E_RESPONSE_BODY)"
assert_equals "201" "$E2E_RESPONSE_STATUS" "POST exec status (post-resume)"
COMMAND_ID=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.command_id')
assert_not_empty "$COMMAND_ID" "command id returned (post-resume)"

EXEC_RESULT=$(wait_exec_complete "$CONTAINER_ID" "$COMMAND_ID" 30) \
	|| e2e_fail "exec did not complete (post-resume)"
EXIT_CODE=$(printf '%s' "$EXEC_RESULT" | jq -r '.exit_code')
STDOUT=$(printf '%s' "$EXEC_RESULT" | jq -r '[.messages[] | select(.stream == "stdout") | .data] | join("")')
STDERR=$(printf '%s' "$EXEC_RESULT" | jq -r '[.messages[] | select(.stream == "stderr") | .data] | join("")')
step_set_exec_result "$EXIT_CODE" "$STDOUT" "$STDERR"
assert_zero "$EXIT_CODE" "exec exit code (post-resume)"
assert_contains "$STDOUT" "hello_drover" "exec stdout (post-resume)"
step_end

# --- 6. destroy ------------------------------------------------------------

step_begin "destroy-container"
api_delete "${ORCHESTRATOR_URL}/containers/${CONTAINER_ID}"
assert_equals "200" "$E2E_RESPONSE_STATUS" "DELETE /containers status"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "destroyed" "$FINAL" "final status is destroyed"
step_end

echo "[test] 06-stop-resume: ok"
