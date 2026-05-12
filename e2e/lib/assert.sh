#!/usr/bin/env bash
# Tiny assertion helpers for the e2e bash test suite.
#
# Each helper prints a one-line PASS/FAIL message and either returns 0 or
# calls `e2e_fail` (which exits non-zero). Tests source this file via
# lib/common.sh and use the assertions to make pass/fail explicit.

# Print a failure message and exit. Honours $E2E_STEP_NAME / $E2E_TEST_NAME
# if set so partial-chunk writers in lib/logs.sh can correlate.
e2e_fail() {
	local msg="$*"
	printf '  FAIL: %s\n' "$msg" >&2
	# Stash failure reason so step_end (if active) can include it in the
	# chunk file's FAILURE section.
	E2E_FAILURE_REASON="$msg"
	export E2E_FAILURE_REASON
	exit 1
}

e2e_pass() {
	local msg="$*"
	printf '  PASS: %s\n' "$msg"
}

assert_equals() {
	local expected="$1" actual="$2" msg="${3:-values differ}"
	if [ "$expected" != "$actual" ]; then
		e2e_fail "$msg (expected='$expected' actual='$actual')"
	fi
	e2e_pass "$msg (== '$expected')"
}

assert_contains() {
	local haystack="$1" needle="$2" msg="${3:-substring missing}"
	if [[ "$haystack" != *"$needle"* ]]; then
		e2e_fail "$msg (looking for '$needle' in: $haystack)"
	fi
	e2e_pass "$msg (contains '$needle')"
}

assert_not_empty() {
	local value="$1" msg="${2:-value was empty}"
	if [ -z "$value" ]; then
		e2e_fail "$msg"
	fi
	e2e_pass "$msg (non-empty)"
}

assert_zero() {
	local code="$1" msg="${2:-exit code was non-zero}"
	if [ "$code" != "0" ]; then
		e2e_fail "$msg (exit_code=$code)"
	fi
	e2e_pass "$msg (exit_code=0)"
}

# Walk a JSON-lines log file (one JSON object per line) and fail if any
# line has level == "ERROR". $1 is the path to the log file; $2 is an
# optional message.
#
# Non-JSON lines (uvicorn's startup banner, the entrypoint's stderr) are
# skipped silently — `fromjson?` returns empty on parse failure.
assert_no_error_lines() {
	local path="$1" msg="${2:-orchestrator emitted ERROR-level log lines}"
	if [ ! -f "$path" ]; then
		e2e_fail "log file not found: $path"
	fi
	local errors
	errors=$(jq -Rrc 'fromjson? | select(.level? == "ERROR")' < "$path" 2>/dev/null || true)
	if [ -n "$errors" ]; then
		printf '  --- ERROR-level log lines ---\n' >&2
		printf '%s\n' "$errors" >&2
		local count
		count=$(printf '%s\n' "$errors" | wc -l | tr -d ' ')
		e2e_fail "$msg ($count lines)"
	fi
	e2e_pass "$msg (none found)"
}
