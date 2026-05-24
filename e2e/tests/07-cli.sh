#!/usr/bin/env bash
# Drive a real orchestrator through the `drover` Go CLI binary.
#
# Where tests 03/04/06 exercise the orchestrator's REST/WS API directly via
# curl, this test exercises the same lifecycle through the command-line
# client a user would actually run: `drover ps`, `drover start`,
# `drover exec`, `drover stop`. It is the only test that builds and invokes
# the CLI, so it owns building the binary (the CI workflow only sets up the
# Go toolchain).
#
# The CLI authenticates entirely through two environment variables:
#   DROVER_API_URL  — the orchestrator base URL (== ORCHESTRATOR_URL)
#   DROVER_API_KEY  — the plain-text bearer token (already in .env.test)
#
# Uses the privileged path (matching test 06) so it runs on any host with
# Docker, avoiding the gVisor requirement of test 04. The image is `builder`
# — the same discoverable image every other container test launches.

# shellcheck source=../lib/common.sh
. "$(dirname "$0")/../lib/common.sh"

echo "[test] 07-cli: drive the orchestrator through the drover CLI"

# Locate the repo root the same way the lib helpers locate $E2E_DIR: relative
# to this script. e2e/tests/07-cli.sh -> ../.. is the repo root.
REPO_ROOT=$(cd -- "$(dirname -- "$0")/../.." && pwd)
DROVER_BIN="$REPO_ROOT/cli/bin/drover"

# The CLI reads these two vars (config.Load); point them at the same stack
# the curl-based tests use. ORCHESTRATOR_URL / DROVER_API_KEY come from
# .env.test (sourced by common.sh).
export DROVER_API_URL="$ORCHESTRATOR_URL"
export DROVER_API_KEY="$DROVER_API_KEY"

# Clean up the container we start even if an assertion aborts midway. The
# ERR trap from common.sh writes the partial chunk; this trap (chained after
# it) is best-effort container teardown so a failed run doesn't leak a
# micro-container. drover destroy is idempotent enough for our needs here.
CLI_CONTAINER_ID=""
cleanup_cli() {
	if [ -n "$CLI_CONTAINER_ID" ]; then
		"$DROVER_BIN" destroy "$CLI_CONTAINER_ID" >/dev/null 2>&1 || true
	fi
}
trap cleanup_cli EXIT

# --- 0. build the binary ---------------------------------------------------

step_begin "build-binary"
echo "  building drover from $REPO_ROOT/cli"
( cd "$REPO_ROOT/cli" && go build -o bin/drover ./cmd/drover ) \
	|| e2e_fail "go build of drover CLI failed"
[ -x "$DROVER_BIN" ] || e2e_fail "drover binary not found at $DROVER_BIN"
e2e_pass "drover binary built"
step_end

# --- 1. ps -----------------------------------------------------------------
# `drover ps` prints a JSON array of containers. We don't assert on its
# contents (other tests may have left state); we assert it exits zero and
# emits valid JSON, proving auth + base URL are wired correctly.

step_begin "ps"
PS_OUT=$("$DROVER_BIN" ps) || e2e_fail "drover ps exited non-zero"
PS_TYPE=$(printf '%s' "$PS_OUT" | jq -r 'type') \
	|| e2e_fail "drover ps did not emit valid JSON"
assert_equals "array" "$PS_TYPE" "drover ps prints a JSON array"
step_end

# --- 2. start --------------------------------------------------------------
# `drover start <image>` blocks until running and prints the container JSON
# (with .id and .status). Use the privileged path so no gVisor is required.

step_begin "start-container"
step_set_wait "running" 30
START_OUT=$("$DROVER_BIN" start builder --privileged) \
	|| e2e_fail "drover start exited non-zero"
CLI_CONTAINER_ID=$(printf '%s' "$START_OUT" | jq -r '.id')
assert_not_empty "$CLI_CONTAINER_ID" "drover start returned a container id"
echo "  container_id=$CLI_CONTAINER_ID"
START_STATUS=$(printf '%s' "$START_OUT" | jq -r '.status')
assert_equals "running" "$START_STATUS" "drover start blocks until running"
step_end

# --- 3. exec ---------------------------------------------------------------
# `drover exec <id> -- <cmd...>` streams newline-delimited JSON frames from
# the per-container WebSocket: output frames then a status:complete frame.
# The CLI exits with the command's exit code. Assert both the streamed
# stdout and the propagated exit code.

step_begin "exec-command"
step_set_wait "complete" 30
EXEC_OUT=$("$DROVER_BIN" exec "$CLI_CONTAINER_ID" -- echo hello) || EXEC_RC=$?
EXEC_RC="${EXEC_RC:-0}"
assert_zero "$EXEC_RC" "drover exec propagated exit code"

# Reassemble stdout from the streamed output frames (one JSON object/line).
EXEC_STDOUT=$(printf '%s' "$EXEC_OUT" \
	| jq -r 'select(.type=="output" and .stream=="stdout") | .data' \
	| tr -d '\n')
assert_contains "$EXEC_STDOUT" "hello" "drover exec streamed stdout"

# The terminal status frame must report a zero exit code.
EXEC_EXIT_CODE=$(printf '%s' "$EXEC_OUT" \
	| jq -r 'select(.type=="status" and .status=="complete") | .exit_code')
assert_zero "$EXEC_EXIT_CODE" "exec status frame exit_code"
step_set_exec_result "$EXEC_EXIT_CODE" "$EXEC_STDOUT" ""
step_end

# --- 4. stop ---------------------------------------------------------------
# `drover stop <id>` blocks until stopped and prints the container JSON.

step_begin "stop-container"
step_set_wait "stopped" 30
STOP_OUT=$("$DROVER_BIN" stop "$CLI_CONTAINER_ID") \
	|| e2e_fail "drover stop exited non-zero"
STOP_STATUS=$(printf '%s' "$STOP_OUT" | jq -r '.status')
assert_equals "stopped" "$STOP_STATUS" "drover stop blocks until stopped"
step_end

# --- 5. destroy ------------------------------------------------------------
# Tidy up explicitly so the assertions cover the full lifecycle; the EXIT
# trap is only a fallback for the failure path.

step_begin "destroy-container"
DESTROY_OUT=$("$DROVER_BIN" destroy "$CLI_CONTAINER_ID") \
	|| e2e_fail "drover destroy exited non-zero"
DESTROY_STATUS=$(printf '%s' "$DESTROY_OUT" | jq -r '.status')
assert_equals "destroyed" "$DESTROY_STATUS" "drover destroy blocks until destroyed"
CLI_CONTAINER_ID=""
step_end

echo "[test] 07-cli: ok"
