#!/usr/bin/env bash
# Stop then resume a privileged worker, asserting the worker is
# reachable again afterwards via the same `echo $DROVER_TEST_VAR` exec
# that test 03 uses. This guards the `stopped --> resuming --> running`
# transition end-to-end: the orchestrator schedules background Docker
# start work, Docker restarts the worker, the worker agent reconnects,
# and once the agent's `ready` arrives the row transitions to `running`.
#
# The resume flow mirrors the initial init flow: status stays in
# `resuming` until the worker agent sends `ready`, so polling for
# `running` is enough — no extra retry on 409 WorkerNotConnected is
# needed.
#
# Uses the privileged path so the test runs on any host with Docker —
# matching test 03 and avoiding the gVisor requirement of test 04.

# shellcheck source=../lib/common.sh
. "$(dirname "$0")/../lib/common.sh"

echo "[test] 06-stop-resume: stop then resume preserves reachability"

# --- 1. create -------------------------------------------------------------

step_begin "create-worker"
step_set_wait "running" 30
REQUEST_BODY='{"image": "builder", "privileged": true, "env": {"DROVER_TEST_VAR": "hello_drover"}}'
api_post "${ORCHESTRATOR_URL}/workers" "$REQUEST_BODY"
assert_equals "201" "$E2E_RESPONSE_STATUS" "POST /workers status"
WORKER_ID=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.id')
assert_not_empty "$WORKER_ID" "worker id returned"
echo "  worker_id=$WORKER_ID"
wait_worker_status "$WORKER_ID" "running" 30 \
	|| e2e_fail "worker did not reach running"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "running" "$FINAL" "final status is running"
step_end

# --- 2. exec (pre-stop) ----------------------------------------------------

step_begin "exec-before-stop"
step_set_wait "complete" 30
EXEC_BODY='{"command": "echo $DROVER_TEST_VAR"}'
api_post "${ORCHESTRATOR_URL}/workers/${WORKER_ID}/execs" "$EXEC_BODY"
assert_equals "201" "$E2E_RESPONSE_STATUS" "POST exec status"
COMMAND_ID=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.command_id')
assert_not_empty "$COMMAND_ID" "command id returned"

EXEC_RESULT=$(wait_exec_complete "$WORKER_ID" "$COMMAND_ID" 30) \
	|| e2e_fail "exec did not complete"
EXIT_CODE=$(printf '%s' "$EXEC_RESULT" | jq -r '.exit_code')
STDOUT=$(printf '%s' "$EXEC_RESULT" | jq -r '[.messages[] | select(.stream == "stdout") | .data] | join("")')
STDERR=$(printf '%s' "$EXEC_RESULT" | jq -r '[.messages[] | select(.stream == "stderr") | .data] | join("")')
step_set_exec_result "$EXIT_CODE" "$STDOUT" "$STDERR"
assert_zero "$EXIT_CODE" "exec exit code"
assert_contains "$STDOUT" "hello_drover" "exec stdout"
step_end

# --- 3. stop ---------------------------------------------------------------

step_begin "stop-worker"
step_set_wait "stopped" 30
api_post "${ORCHESTRATOR_URL}/workers/${WORKER_ID}/stop"
if [ "$E2E_RESPONSE_STATUS" != "200" ] && [ "$E2E_RESPONSE_STATUS" != "202" ]; then
	e2e_fail "POST /workers/{id}/stop returned $E2E_RESPONSE_STATUS"
fi
wait_worker_status "$WORKER_ID" "stopped" 30 \
	|| e2e_fail "worker did not reach stopped"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "stopped" "$FINAL" "final status is stopped"
step_end

# --- 4. resume -------------------------------------------------------------

step_begin "resume-worker"
step_set_wait "resuming" 5
api_post "${ORCHESTRATOR_URL}/workers/${WORKER_ID}/resume"
if [ "$E2E_RESPONSE_STATUS" != "200" ] && [ "$E2E_RESPONSE_STATUS" != "202" ]; then
	e2e_fail "POST /workers/{id}/resume returned $E2E_RESPONSE_STATUS"
fi
# The resume call returns immediately with status `resuming` (mirroring
# init).  The Docker start and the worker agent's ready handshake happen
# in the background.
IMMEDIATE=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "resuming" "$IMMEDIATE" "resume returned status resuming"
step_end

# --- 5. wait for running (ready handshake) --------------------------------

step_begin "wait-for-ready"
step_set_wait "running" 30
wait_worker_status "$WORKER_ID" "running" 30 \
	|| e2e_fail "worker did not reach running after resume"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "running" "$FINAL" "final status is running after resume"
step_end

# --- 6. exec (post-resume) -------------------------------------------------
# By the time status is `running`, the worker agent has connected and sent
# `ready`, so the exec POST should be accepted immediately — no retry on
# 409 WorkerNotConnected is needed.

step_begin "exec-after-resume"
step_set_wait "complete" 30
EXEC_BODY='{"command": "echo $DROVER_TEST_VAR"}'
api_post "${ORCHESTRATOR_URL}/workers/${WORKER_ID}/execs" "$EXEC_BODY"
assert_equals "201" "$E2E_RESPONSE_STATUS" "POST exec status (post-resume)"
COMMAND_ID=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.command_id')
assert_not_empty "$COMMAND_ID" "command id returned (post-resume)"

EXEC_RESULT=$(wait_exec_complete "$WORKER_ID" "$COMMAND_ID" 30) \
	|| e2e_fail "exec did not complete (post-resume)"
EXIT_CODE=$(printf '%s' "$EXEC_RESULT" | jq -r '.exit_code')
STDOUT=$(printf '%s' "$EXEC_RESULT" | jq -r '[.messages[] | select(.stream == "stdout") | .data] | join("")')
STDERR=$(printf '%s' "$EXEC_RESULT" | jq -r '[.messages[] | select(.stream == "stderr") | .data] | join("")')
step_set_exec_result "$EXIT_CODE" "$STDOUT" "$STDERR"
assert_zero "$EXIT_CODE" "exec exit code (post-resume)"
assert_contains "$STDOUT" "hello_drover" "exec stdout (post-resume)"
step_end

# --- 7. destroy ------------------------------------------------------------

step_begin "destroy-worker"
api_delete "${ORCHESTRATOR_URL}/workers/${WORKER_ID}"
assert_equals "200" "$E2E_RESPONSE_STATUS" "DELETE /workers status"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "destroyed" "$FINAL" "final status is destroyed"
step_end

echo "[test] 06-stop-resume: ok"
