#!/usr/bin/env bash
# Worker-log retention checks that need a dedicated worker.
#
# Tests 03 and 04 already cover the post-exec / post-destroy log-files
# assertions for a single happy-path worker.  This test exists to
# exercise the case the others can't:
#
#   Stop / resume continuity: stop a running worker, resume it, and
#   assert that the captured 0.log contains output from BOTH segments.
#   The drover-executor logs "Connecting to /var/run/drover/sockets/orchestrator.sock" each
#   time it (re)starts, so we look for that line appearing at least
#   twice across the file set.
#
# Init-window capture and init-timeout retention are deferred to a
# follow-up because they require a custom image whose entrypoint
# delays before exec'ing the agent (or never execs it).  They are
# already covered by the unit tests in tests/test_worker_manager.py
# (test_init_starts_log_capture, test_fail_init_persists_cursor_and_
# keeps_directory).

# shellcheck source=../lib/common.sh
. "$(dirname "$0")/../lib/common.sh"

echo "[test] 05-log-retention: stop/resume continuity"

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
step_end

# --- 2. capture first segment ---------------------------------------------

step_begin "capture-first-segment"
# Wait briefly so the executor has flushed its connect log.
sleep 1
assert_log_files_contains "$WORKER_ID" "0.log"
assert_log_file_contains "$WORKER_ID" "0.log" "Connecting to"
# Snapshot the captured size so we can verify it grows after resume.
api_get "${ORCHESTRATOR_URL}/workers/${WORKER_ID}/logs/files/0.log"
INITIAL_SIZE=$(printf '%s' "$E2E_RESPONSE_BODY" | wc -c | tr -d ' ')
echo "  initial_0.log_size=$INITIAL_SIZE"
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
step_end

# --- 4. resume and capture second segment ---------------------------------

step_begin "resume-worker"
step_set_wait "running" 30
api_post "${ORCHESTRATOR_URL}/workers/${WORKER_ID}/resume"
if [ "$E2E_RESPONSE_STATUS" != "200" ] && [ "$E2E_RESPONSE_STATUS" != "202" ]; then
	e2e_fail "POST /workers/{id}/resume returned $E2E_RESPONSE_STATUS"
fi
wait_worker_status "$WORKER_ID" "running" 30 \
	|| e2e_fail "worker did not reach running on resume"
sleep 1
api_get "${ORCHESTRATOR_URL}/workers/${WORKER_ID}/logs/files/0.log"
assert_equals "200" "$E2E_RESPONSE_STATUS" "GET 0.log after resume"
RESUMED_SIZE=$(printf '%s' "$E2E_RESPONSE_BODY" | wc -c | tr -d ' ')
echo "  resumed_0.log_size=$RESUMED_SIZE"
if [ "$RESUMED_SIZE" -le "$INITIAL_SIZE" ]; then
	e2e_fail "0.log did not grow after resume (initial=$INITIAL_SIZE resumed=$RESUMED_SIZE)"
fi
e2e_pass "0.log grew across stop/resume ($INITIAL_SIZE -> $RESUMED_SIZE)"
# The executor logs "Connecting to" each time it (re)connects; after a
# resume we expect at least two such lines across the file set.
CONNECT_COUNT=$(printf '%s' "$E2E_RESPONSE_BODY" | grep -o "Connecting to" | wc -l | tr -d ' ')
if [ "$CONNECT_COUNT" -lt 2 ]; then
	e2e_fail "expected >=2 'Connecting to' lines after resume, got $CONNECT_COUNT"
fi
e2e_pass "found $CONNECT_COUNT 'Connecting to' lines across the segments"
step_end

# --- 5. cleanup ------------------------------------------------------------

step_begin "destroy-worker"
api_delete "${ORCHESTRATOR_URL}/workers/${WORKER_ID}"
assert_equals "200" "$E2E_RESPONSE_STATUS" "DELETE /workers status"
assert_log_files_empty "$WORKER_ID"
step_end

echo "[test] 05-log-retention: ok"
