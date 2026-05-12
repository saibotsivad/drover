#!/usr/bin/env bash
# Per-step log capture for the e2e test suite.
#
# Each logical test step is bracketed by `step_begin <name>` and
# `step_end` (or `step_end_failure <reason>` when a partial chunk is being
# written from a trap). step_end pulls the orchestrator and webapp logs
# for the time window the step occupied and writes a single self-contained
# chunk file under $E2E_RUN_LOG_DIR/<test>/<NN-step>.log.
#
# The chunk format is plain text with labelled sections, readable by both
# humans and automated systems without preprocessing. It is described in
# detail in docs/full-e2e-suite.md.

# Grace period (in milliseconds) added to the end of the capture window
# so log lines that flush slightly after the API call's terminal state
# are still included.
E2E_LOG_GRACE_MS="${E2E_LOG_GRACE_MS:-500}"

# Reset per-step state. Called at the top of step_begin and step_end so
# stale data from a previous step never leaks into the next chunk.
_reset_step_state() {
	E2E_STEP_INDEX="${E2E_STEP_INDEX:-0}"
	E2E_STEP_NAME=""
	E2E_STEP_T1=""
	E2E_STEP_T1_DATE=""
	E2E_STEP_STARTED_HUMAN=""
	E2E_LAST_REQUEST_METHOD=""
	E2E_LAST_REQUEST_URL=""
	E2E_LAST_REQUEST_BODY=""
	E2E_RESPONSE_STATUS=""
	E2E_RESPONSE_BODY=""
	E2E_LAST_POLL_COUNT=""
	E2E_LAST_POLL_ELAPSED_MS=""
	E2E_STEP_WAIT_TARGET=""
	E2E_STEP_WAIT_TIMEOUT=""
	E2E_STEP_EXEC_RESULT=""
	E2E_FAILURE_REASON=""
}

# Begin a logical step. $1 is the slug used in the chunk filename. Records
# T1 (the moment before any API call) and bumps the step index counter
# scoped to the current test.
step_begin() {
	_reset_step_state
	E2E_STEP_INDEX=$(( ${E2E_STEP_INDEX_COUNTER:-0} + 1 ))
	E2E_STEP_INDEX_COUNTER="$E2E_STEP_INDEX"
	E2E_STEP_NAME="$1"
	# Two formats: ISO for the chunk file, and "--since" parseable for
	# docker logs.
	E2E_STEP_T1=$(date -u +%s%3N)
	# RFC3339 with explicit Z so docker logs --since interprets it as UTC
	# regardless of the host's local timezone.
	E2E_STEP_T1_DATE=$(date -u -d "@$(( E2E_STEP_T1 / 1000 ))" +'%Y-%m-%dT%H:%M:%SZ')
	E2E_STEP_STARTED_HUMAN=$(date -u +'%Y-%m-%dT%H:%M:%S.%3NZ')
	echo "[step] $E2E_STEP_NAME"
}

# Record the wait target and timeout for the WAIT section of the chunk.
# Optional; tests that don't poll can skip it.
step_set_wait() {
	E2E_STEP_WAIT_TARGET="$1"
	E2E_STEP_WAIT_TIMEOUT="$2"
}

# Record an exec result (exit_code, stdout, stderr) so step_end can emit
# the EXEC RESULT section. $1 = exit_code, $2 = stdout, $3 = stderr.
step_set_exec_result() {
	E2E_STEP_EXEC_RESULT=$(printf 'exit_code: %s\nstdout: %s\nstderr: %s' "$1" "${2:-(none)}" "${3:-(none)}")
}

# Finish a step successfully. Pulls logs for [T1, T2+grace] and writes
# the chunk file. Caller should already have asserted everything they care
# about; this function only writes diagnostics.
step_end() {
	_write_chunk "PASS" ""
	_reset_step_state
}

# Finish a step with a failure. Writes a partial chunk that includes a
# FAILURE section with $1 as the reason.
step_end_failure() {
	local reason="${1:-unknown failure}"
	_write_chunk "FAIL" "$reason"
	_reset_step_state
}

_write_chunk() {
	local result="$1" failure_reason="$2"

	# A step_end was called without a matching step_begin — write nothing
	# rather than producing a half-built chunk.
	if [ -z "$E2E_STEP_NAME" ]; then
		return 0
	fi

	local t2_ms t2_iso
	t2_ms=$(date -u +%s%3N)
	t2_iso=$(date -u +'%Y-%m-%dT%H:%M:%S.%3NZ')

	# docker logs --since / --until accept fractional seconds in
	# RFC3339-ish form. We extend the upper bound by the grace window so
	# trailing lines aren't truncated.
	local until_ms=$(( t2_ms + E2E_LOG_GRACE_MS ))
	local until_iso
	until_iso=$(date -u -d "@$(( until_ms / 1000 ))" +'%Y-%m-%dT%H:%M:%SZ')

	mkdir -p "$E2E_RUN_LOG_DIR/$E2E_TEST_NAME"
	local chunk_path
	chunk_path=$(printf '%s/%s/%02d-%s.log' "$E2E_RUN_LOG_DIR" "$E2E_TEST_NAME" "$E2E_STEP_INDEX" "$E2E_STEP_NAME")

	{
		printf 'STEP:      %s\n' "$E2E_STEP_NAME"
		printf 'TEST:      %s\n' "$E2E_TEST_NAME"
		printf 'RUN_ID:    %s\n' "$E2E_RUN_ID"
		printf 'STARTED:   %s\n' "$E2E_STEP_STARTED_HUMAN"
		printf 'COMPLETED: %s\n' "$t2_iso"
		printf 'RESULT:    %s\n' "$result"
		printf '\n'

		printf -- '--- REQUEST ---\n'
		if [ -n "$E2E_LAST_REQUEST_METHOD" ]; then
			printf '%s %s\n' "$E2E_LAST_REQUEST_METHOD" "$E2E_LAST_REQUEST_URL"
			if [ -n "$E2E_LAST_REQUEST_BODY" ]; then
				printf '%s\n' "$E2E_LAST_REQUEST_BODY"
			fi
		else
			printf '(none)\n'
		fi
		printf '\n'

		printf -- '--- RESPONSE ---\n'
		if [ -n "$E2E_RESPONSE_STATUS" ]; then
			printf 'HTTP %s\n' "$E2E_RESPONSE_STATUS"
			if [ -n "$E2E_RESPONSE_BODY" ]; then
				printf '%s\n' "$E2E_RESPONSE_BODY"
			fi
		else
			printf '(none)\n'
		fi
		printf '\n'

		if [ -n "$E2E_STEP_WAIT_TARGET" ]; then
			printf -- '--- WAIT ---\n'
			printf 'target_status: %s\n' "$E2E_STEP_WAIT_TARGET"
			printf 'timeout: %ss\n' "$E2E_STEP_WAIT_TIMEOUT"
			if [ -n "$E2E_LAST_POLL_ELAPSED_MS" ]; then
				printf 'elapsed: %sms\n' "$E2E_LAST_POLL_ELAPSED_MS"
			fi
			if [ -n "$E2E_LAST_POLL_COUNT" ]; then
				printf 'polls: %s\n' "$E2E_LAST_POLL_COUNT"
			fi
			printf '\n'
		fi

		if [ -n "$E2E_STEP_EXEC_RESULT" ]; then
			printf -- '--- EXEC RESULT ---\n'
			printf '%s\n' "$E2E_STEP_EXEC_RESULT"
			printf '\n'
		fi

		printf -- '--- ORCHESTRATOR LOGS ---\n'
		_dump_container_logs "$ORCHESTRATOR_CONTAINER" "$E2E_STEP_T1_DATE" "$until_iso"
		printf '\n'

		printf -- '--- WEBAPP LOGS ---\n'
		_dump_container_logs "$WEBAPP_CONTAINER" "$E2E_STEP_T1_DATE" "$until_iso"
		printf '\n'

		printf -- '--- FAILURE ---\n'
		if [ -n "$failure_reason" ]; then
			printf '%s\n' "$failure_reason"
		else
			printf '(none)\n'
		fi
	} > "$chunk_path"

	# Tee one-line summary for the run-level summary.log.
	printf '%s %s/%02d-%s -> %s\n' \
		"$t2_iso" "$E2E_TEST_NAME" "$E2E_STEP_INDEX" "$E2E_STEP_NAME" "$result" \
		>> "$E2E_RUN_LOG_DIR/summary.log"
}

# Stream the orchestrator container log to a fresh file. Used by the
# whole-suite assertion that checks for ERROR-level lines after all
# tests have run.
dump_orchestrator_log_to() {
	local out="$1"
	docker logs "$ORCHESTRATOR_CONTAINER" >"$out" 2>>"$out" || true
}

_dump_container_logs() {
	local container="$1" since="$2" until="$3"
	if ! docker inspect "$container" >/dev/null 2>&1; then
		printf '(container %s not running)\n' "$container"
		return 0
	fi
	# docker logs writes JSON-lines to stderr in our images (the JSON log
	# formatter writes to a StreamHandler whose default is stderr). Merge
	# both streams so the chunk captures everything.
	local out
	if ! out=$(docker logs --since "$since" --until "$until" "$container" 2>&1); then
		printf '(docker logs failed for %s)\n' "$container"
		return 0
	fi
	if [ -z "$out" ]; then
		printf '(none in window)\n'
	else
		printf '%s\n' "$out"
	fi
}

# ERR trap installed by tests: if anything unexpected fails between
# step_begin and step_end, this writes a partial chunk so the failure
# isn't silently lost. The trap is one-shot per step — once it fires it
# disables itself by clearing E2E_STEP_NAME.
e2e_err_trap() {
	local code=$?
	if [ -z "$E2E_STEP_NAME" ]; then
		exit "$code"
	fi
	local reason="${E2E_FAILURE_REASON:-unexpected error (exit $code) in step '$E2E_STEP_NAME'}"
	step_end_failure "$reason"
	exit "$code"
}
