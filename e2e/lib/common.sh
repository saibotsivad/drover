#!/usr/bin/env bash
# Common setup sourced by every test script.
#
# Tests source this file (instead of repeating boilerplate) to get:
# - strict mode (set -Eeuo pipefail)
# - the e2e/.env.test environment loaded
# - lib/{assert,api,logs}.sh helpers
# - an ERR trap that writes a partial log chunk on unexpected failure
# - $E2E_TEST_NAME / $E2E_RUN_ID / $E2E_RUN_LOG_DIR populated

set -Eeuo pipefail

E2E_LIB_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
E2E_DIR=$(cd -- "$E2E_LIB_DIR/.." && pwd)

# Pull deterministic test config into the environment. Variables defined
# in .env.test are exported for the duration of the test run.
set -a
# shellcheck disable=SC1091
. "$E2E_DIR/.env.test"
set +a

# shellcheck disable=SC1091
. "$E2E_LIB_DIR/assert.sh"
# shellcheck disable=SC1091
. "$E2E_LIB_DIR/api.sh"
# shellcheck disable=SC1091
. "$E2E_LIB_DIR/logs.sh"
# shellcheck disable=SC1091
. "$E2E_LIB_DIR/log_files.sh"
# shellcheck disable=SC1091
. "$E2E_LIB_DIR/ws.sh"

# Test scripts get their name from the basename of $0 minus the .sh
# extension. The runner sets $E2E_RUN_ID and $E2E_RUN_LOG_DIR before
# invoking us; fall back to a fresh run dir if a developer is running a
# single test directly (./e2e/tests/03-...sh).
if [ -z "${E2E_RUN_ID:-}" ]; then
	E2E_RUN_ID=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
	export E2E_RUN_ID
fi
if [ -z "${E2E_RUN_LOG_DIR:-}" ]; then
	E2E_RUN_LOG_DIR="$E2E_DIR/logs/$E2E_RUN_ID"
	export E2E_RUN_LOG_DIR
	mkdir -p "$E2E_RUN_LOG_DIR"
fi
E2E_TEST_NAME=$(basename "$0" .sh)
export E2E_TEST_NAME
# Each test starts fresh with step index 0; step_begin increments it.
E2E_STEP_INDEX_COUNTER=0
export E2E_STEP_INDEX_COUNTER

# Install the ERR trap. set -E propagates the trap into functions and
# subshells.
trap 'e2e_err_trap' ERR
