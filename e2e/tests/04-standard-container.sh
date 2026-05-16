#!/usr/bin/env bash
# Same lifecycle as test 03, but for a non-privileged container running
# under gVisor (the runsc runtime). The orchestrator must be configured
# with a non-builder discoverable image — by convention this test uses
# `builder` because the same image carries `drover.name=builder` and runs
# the executor without needing the host Docker socket bind-mount (the
# orchestrator only mounts /run/docker.sock into privileged containers).
#
# If runsc is not installed on the host, gVisor cannot run. By default
# this test FAILS in that case, because a silent skip would let an
# accidentally-missing gVisor install pass as a green run. To opt into
# skipping when runsc really isn't available (and you know you're not
# testing the non-privileged path), set E2E_ALLOW_MISSING_RUNSC=1.

# shellcheck source=../lib/common.sh
. "$(dirname "$0")/../lib/common.sh"

echo "[test] 04-standard-container: non-privileged lifecycle under gVisor"

if ! command -v runsc >/dev/null 2>&1; then
	if [ "${E2E_ALLOW_MISSING_RUNSC:-}" = "1" ]; then
		echo "  SKIP: gVisor (runsc) not installed; skipped because E2E_ALLOW_MISSING_RUNSC=1"
		exit 0
	fi
	echo "  FAIL: gVisor (runsc) is not installed on this host." >&2
	echo "        Install it via docs/install-runsc-gvisor.md, or rerun with" >&2
	echo "        E2E_ALLOW_MISSING_RUNSC=1 to skip this test explicitly." >&2
	exit 1
fi

# --- 1. create -------------------------------------------------------------

step_begin "create-container"
step_set_wait "running" 30
REQUEST_BODY='{"image": "builder", "privileged": false, "env": {"DROVER_TEST_VAR": "hello_drover"}}'
api_post "${ORCHESTRATOR_URL}/containers" "$REQUEST_BODY"
assert_equals "201" "$E2E_RESPONSE_STATUS" "POST /containers status"
CONTAINER_ID=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.id')
assert_not_empty "$CONTAINER_ID" "container id returned"
echo "  container_id=$CONTAINER_ID"
if ! wait_container_status "$CONTAINER_ID" "running" 45; then
	# If the container went to error, surface error_code in the chunk so
	# missing --host-uds=all (the gVisor flag the orchestrator needs) is
	# immediately diagnosable.
	ERROR_CODE=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.error_code // empty')
	if [ -n "$ERROR_CODE" ]; then
		e2e_fail "non-privileged container failed: error_code=$ERROR_CODE (check that runsc daemon.json includes --host-uds=all)"
	fi
	e2e_fail "non-privileged container did not reach running"
fi
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "running" "$FINAL" "final status is running"
step_end

# --- 2. exec ---------------------------------------------------------------

step_begin "exec-command"
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

# --- 2b. captured-log files check -----------------------------------------

step_begin "assert-captured-logs"
assert_log_files_contains "$CONTAINER_ID" "0.log"
assert_log_file_contains "$CONTAINER_ID" "0.log" "Connecting to"
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

# --- 4. destroy and verify logs are discarded ------------------------------

step_begin "destroy-container"
api_delete "${ORCHESTRATOR_URL}/containers/${CONTAINER_ID}"
assert_equals "200" "$E2E_RESPONSE_STATUS" "DELETE /containers status"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "destroyed" "$FINAL" "final status is destroyed"
assert_log_files_empty "$CONTAINER_ID"
assert_log_file_missing "$CONTAINER_ID" "0.log"
step_end

echo "[test] 04-standard-container: ok"
