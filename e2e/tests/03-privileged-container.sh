#!/usr/bin/env bash
# Full privileged-container lifecycle test.
#
# Steps:
#   1. POST /containers (image=builder, privileged=true, env DROVER_TEST_VAR)
#   2. Poll until status == running
#   3. POST /containers/{id}/exec with `echo $DROVER_TEST_VAR`
#   4. Poll exec status until complete; assert exit_code 0 and stdout
#      contains "hello_drover"
#   5. POST /containers/{id}/stop
#   6. Poll until status == stopped
#   7. Walk the captured orchestrator log and assert no ERROR-level lines
#
# This single test exercises the socket protocol, state machine, and log
# output in one pass. It only depends on the privileged path so it runs
# on any host with Docker — gVisor is not required.

# shellcheck source=../lib/common.sh
. "$(dirname "$0")/../lib/common.sh"

echo "[test] 03-privileged-container: privileged lifecycle"

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

# --- 2. exec ---------------------------------------------------------------

step_begin "exec-command"
step_set_wait "complete" 30
EXEC_BODY='{"command": "echo $DROVER_TEST_VAR"}'
api_post "${ORCHESTRATOR_URL}/containers/${CONTAINER_ID}/exec" "$EXEC_BODY"
assert_equals "201" "$E2E_RESPONSE_STATUS" "POST exec status"
COMMAND_ID=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.command_id')
assert_not_empty "$COMMAND_ID" "command id returned"
echo "  command_id=$COMMAND_ID"

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
# The stop call returns the container row immediately; we still poll for
# the terminal status because the actual `docker stop` happens in the
# background.
if [ "$E2E_RESPONSE_STATUS" != "200" ] && [ "$E2E_RESPONSE_STATUS" != "202" ]; then
	e2e_fail "POST /containers/{id}/stop returned $E2E_RESPONSE_STATUS"
fi
wait_container_status "$CONTAINER_ID" "stopped" 30 \
	|| e2e_fail "container did not reach stopped"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "stopped" "$FINAL" "final status is stopped"
step_end

# --- 4. log assertion ------------------------------------------------------

step_begin "assert-no-errors"
ORCH_LOG="$E2E_RUN_LOG_DIR/orchestrator.full.log"
dump_orchestrator_log_to "$ORCH_LOG"
assert_no_error_lines "$ORCH_LOG" "orchestrator log clean of ERROR lines"
step_end

echo "[test] 03-privileged-container: ok"
