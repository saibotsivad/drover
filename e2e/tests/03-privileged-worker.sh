#!/usr/bin/env bash
# Full privileged-worker lifecycle test.
#
# Steps:
#   1. POST /workers (image=builder, privileged=true, env DROVER_TEST_VAR)
#   2. Poll until status == running
#   3. POST /workers/{id}/execs with `echo $DROVER_TEST_VAR`
#   4. Poll exec status until complete; assert exit_code 0 and stdout
#      contains "hello_drover"
#   5. WebSocket auth: no token / bad token / unknown worker /
#      invalid tail are rejected; valid Bearer header and ?token=
#      query param are accepted.
#   6. WebSocket streaming: connect via WS, POST a second exec, and
#      verify the {output,status} JSON frames the SocketManager
#      broadcasts plus at least one Docker {log} frame arrive over
#      the same connection.
#   7. POST /workers/{id}/stop
#   8. Poll until status == stopped
#   9. Walk the captured orchestrator log and assert no ERROR-level lines
#
# This single test exercises the socket protocol, state machine, log
# output, and the per-worker WebSocket endpoint in one pass. It only
# depends on the privileged path so it runs on any host with Docker —
# gVisor is not required.

# shellcheck source=../lib/common.sh
. "$(dirname "$0")/../lib/common.sh"

echo "[test] 03-privileged-worker: privileged lifecycle"

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

# --- 2. exec ---------------------------------------------------------------

step_begin "exec-command"
step_set_wait "complete" 30
EXEC_BODY='{"command": "echo $DROVER_TEST_VAR"}'
api_post "${ORCHESTRATOR_URL}/workers/${WORKER_ID}/execs" "$EXEC_BODY"
assert_equals "201" "$E2E_RESPONSE_STATUS" "POST exec status"
COMMAND_ID=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.command_id')
assert_not_empty "$COMMAND_ID" "command id returned"
echo "  command_id=$COMMAND_ID"

EXEC_RESULT=$(wait_exec_complete "$WORKER_ID" "$COMMAND_ID" 30) \
	|| e2e_fail "exec did not complete"
EXIT_CODE=$(printf '%s' "$EXEC_RESULT" | jq -r '.exit_code')
STDOUT=$(printf '%s' "$EXEC_RESULT" | jq -r '[.messages[] | select(.stream == "stdout") | .data] | join("")')
STDERR=$(printf '%s' "$EXEC_RESULT" | jq -r '[.messages[] | select(.stream == "stderr") | .data] | join("")')
step_set_exec_result "$EXIT_CODE" "$STDOUT" "$STDERR"
assert_zero "$EXIT_CODE" "exec exit code"
assert_contains "$STDOUT" "hello_drover" "exec stdout"
step_end

# --- 2b. captured-log files check -----------------------------------------
#
# The drover-executor writes "Connecting to /var/run/drover/sockets/orchestrator.sock" to
# stderr the moment it starts up, which the orchestrator captures into
# 0.log via the Docker log stream.  We assert on that sentinel because
# the exec stdout itself is piped through the socket and never reaches
# worker stdout.

step_begin "assert-captured-logs"
assert_log_files_contains "$WORKER_ID" "0.log"
assert_log_file_contains "$WORKER_ID" "0.log" "Connecting to"
step_end

# --- 2c. WebSocket authentication checks ----------------------------------
#
# The per-worker WS endpoint authenticates inline (HTTP middleware
# doesn't run for the upgrade), accepting either an "Authorization:
# Bearer …" header or a "?token=…" query param. All four rejection
# paths close BEFORE accept(), which Starlette surfaces as an HTTP 403
# on the upgrade response.

WS_BASE=$(ws_url_base)

step_begin "ws-rejects-missing-auth"
ws_handshake "$WS_BASE/workers/${WORKER_ID}/ws"
assert_equals "403" "$WS_HANDSHAKE_STATUS" "missing auth rejected at handshake"
step_end

step_begin "ws-rejects-bad-bearer-token"
ws_handshake "$WS_BASE/workers/${WORKER_ID}/ws" \
	-H "Authorization: Bearer not-the-real-key"
assert_equals "403" "$WS_HANDSHAKE_STATUS" "bad bearer token rejected at handshake"
step_end

step_begin "ws-rejects-bad-token-query"
ws_handshake "$WS_BASE/workers/${WORKER_ID}/ws?token=not-the-real-key"
assert_equals "403" "$WS_HANDSHAKE_STATUS" "bad ?token= rejected at handshake"
step_end

step_begin "ws-rejects-unknown-worker"
ws_handshake "$WS_BASE/workers/cnt_does_not_exist/ws" \
	-H "Authorization: Bearer $DROVER_API_KEY"
assert_equals "403" "$WS_HANDSHAKE_STATUS" "unknown worker rejected at handshake"
step_end

step_begin "ws-rejects-invalid-tail"
ws_handshake "$WS_BASE/workers/${WORKER_ID}/ws?tail=notanumber" \
	-H "Authorization: Bearer $DROVER_API_KEY"
assert_equals "403" "$WS_HANDSHAKE_STATUS" "invalid ?tail= rejected at handshake"
step_end

step_begin "ws-accepts-valid-bearer-header"
ws_handshake "$WS_BASE/workers/${WORKER_ID}/ws" \
	-H "Authorization: Bearer $DROVER_API_KEY"
assert_equals "101" "$WS_HANDSHAKE_STATUS" "valid bearer header accepted"
step_end

step_begin "ws-accepts-valid-token-query"
ws_handshake "$WS_BASE/workers/${WORKER_ID}/ws?token=$DROVER_API_KEY"
assert_equals "101" "$WS_HANDSHAKE_STATUS" "valid ?token= accepted"
step_end

# --- 2d. WebSocket streaming ----------------------------------------------
#
# Open a WS client in the background, POST a second exec, and verify
# we receive {type:output, command_id, data:hello_drover}, the
# matching {type:status, status:complete, exit_code:0}, plus at least
# one {type:log} frame from the Docker log stream.

step_begin "ws-streams-exec-and-logs"
ws_start_background "$WS_BASE/workers/${WORKER_ID}/ws" \
	-H "Authorization: Bearer $DROVER_API_KEY" \
	--timeout 10 --max-messages 200
# Let the WS handler register its broadcast queue before the exec
# fires; otherwise the SocketManager's broadcast would happen with
# no subscribers and we'd see no output frame.
sleep 1

EXEC_BODY='{"command": "echo $DROVER_TEST_VAR"}'
api_post "${ORCHESTRATOR_URL}/workers/${WORKER_ID}/execs" "$EXEC_BODY"
assert_equals "201" "$E2E_RESPONSE_STATUS" "POST exec status (for WS stream)"
WS_COMMAND_ID=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.command_id')
assert_not_empty "$WS_COMMAND_ID" "command id returned (for WS stream)"
echo "  ws_command_id=$WS_COMMAND_ID"

# Wait for the exec to complete so the SocketManager has broadcast
# both the output and the terminal status frame.
wait_exec_complete "$WORKER_ID" "$WS_COMMAND_ID" 15 >/dev/null \
	|| e2e_fail "exec did not complete (for WS stream)"

# Drain the background client and let it hit --timeout so any
# straggler frames land in $WS_OUT.
ws_wait 12

assert_equals "101" "$WS_HANDSHAKE_STATUS" "WS handshake for streaming was accepted"

OUTPUT_MSG=$(jq -c --arg cid "$WS_COMMAND_ID" \
	'select(.type=="output" and .command_id==$cid and (.data|contains("hello_drover")))' \
	"$WS_OUT" 2>/dev/null | head -1)
assert_not_empty "$OUTPUT_MSG" "received output frame with hello_drover for $WS_COMMAND_ID"

STATUS_MSG=$(jq -c --arg cid "$WS_COMMAND_ID" \
	'select(.type=="status" and .command_id==$cid and .status=="complete")' \
	"$WS_OUT" 2>/dev/null | head -1)
assert_not_empty "$STATUS_MSG" "received status:complete frame for $WS_COMMAND_ID"
WS_STATUS_EXIT=$(printf '%s' "$STATUS_MSG" | jq -r '.exit_code')
assert_equals "0" "$WS_STATUS_EXIT" "status frame exit_code is 0"

LOG_MSG=$(jq -c 'select(.type=="log")' "$WS_OUT" 2>/dev/null | head -1)
assert_not_empty "$LOG_MSG" "received at least one Docker log frame"

rm -f "$WS_OUT" "$WS_ERR"
step_end

# --- 3. stop ---------------------------------------------------------------

step_begin "stop-worker"
step_set_wait "stopped" 30
api_post "${ORCHESTRATOR_URL}/workers/${WORKER_ID}/stop"
# The stop call returns the worker row immediately; we still poll for
# the terminal status because the actual `docker stop` happens in the
# background.
if [ "$E2E_RESPONSE_STATUS" != "200" ] && [ "$E2E_RESPONSE_STATUS" != "202" ]; then
	e2e_fail "POST /workers/{id}/stop returned $E2E_RESPONSE_STATUS"
fi
wait_worker_status "$WORKER_ID" "stopped" 30 \
	|| e2e_fail "worker did not reach stopped"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "stopped" "$FINAL" "final status is stopped"
step_end

# --- 4. destroy and verify logs are discarded ------------------------------

step_begin "destroy-worker"
api_delete "${ORCHESTRATOR_URL}/workers/${WORKER_ID}"
assert_equals "200" "$E2E_RESPONSE_STATUS" "DELETE /workers status"
FINAL=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status')
assert_equals "destroyed" "$FINAL" "final status is destroyed"
# After discard, the row still exists but the capture directory is gone:
# list returns []; the specific file returns 404.
assert_log_files_empty "$WORKER_ID"
assert_log_file_missing "$WORKER_ID" "0.log"
step_end

# --- 5. log assertion ------------------------------------------------------

step_begin "assert-no-errors"
ORCH_LOG="$E2E_RUN_LOG_DIR/orchestrator.full.log"
dump_orchestrator_log_to "$ORCH_LOG"
assert_no_error_lines "$ORCH_LOG" "orchestrator log clean of ERROR lines"
step_end

echo "[test] 03-privileged-worker: ok"
