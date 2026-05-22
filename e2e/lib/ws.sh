#!/usr/bin/env bash
# WebSocket testing helpers for the e2e suite.
#
# The bash tests run on the host and talk to the orchestrator over
# localhost. To exercise the per-container WebSocket endpoint we need a
# WS client; we use the `websockets` Python package inside a dedicated
# venv at $E2E_WS_VENV so the host's system Python isn't touched.
#
# Public functions:
#   ws_url_base                Echoes ws://… (or wss://…) for ORCHESTRATOR_URL.
#   ws_ensure_client           Lazily creates $E2E_WS_VENV with websockets installed.
#   ws_handshake URL [-H H:V…] Connects and reports just the handshake outcome.
#                              Populates $WS_HANDSHAKE_STATUS (101, 403, …).
#   ws_read URL [-H H:V…] -- --timeout SECS --max-messages N
#                              Connects, reads up to N messages or until timeout,
#                              writes each as one JSON line to $WS_OUT (a temp
#                              file). $WS_HANDSHAKE_STATUS is also populated.
#   ws_start_background URL [-H H:V…] -- --timeout SECS --max-messages N
#                              Same as ws_read but runs in the background; sets
#                              $WS_PID, $WS_OUT, $WS_ERR.
#   ws_wait [timeout_secs]     Wait for the backgrounded client to finish.
#                              Populates $WS_HANDSHAKE_STATUS / $WS_CLOSE_CODE.

E2E_WS_VENV="${E2E_WS_VENV:-$E2E_DIR/.venv-ws}"

ws_url_base() {
	# Replace the URL scheme; the rest of the URL is preserved as-is.
	printf '%s' "${ORCHESTRATOR_URL/http:/ws:}" | sed 's|^https:|wss:|'
}

ws_ensure_client() {
	if [ -x "$E2E_WS_VENV/bin/python3" ] \
		&& "$E2E_WS_VENV/bin/python3" -c "import websockets" 2>/dev/null; then
		return 0
	fi
	echo "  setting up websockets venv at $E2E_WS_VENV"
	python3 -m venv "$E2E_WS_VENV" >/dev/null
	"$E2E_WS_VENV/bin/pip" install --quiet --no-input websockets >/dev/null
}

# Internal: run the python client with the given args, writing messages
# to $WS_OUT (caller-supplied) and status to $WS_ERR (caller-supplied).
_ws_run() {
	local out="$1" err="$2"
	shift 2
	ws_ensure_client
	"$E2E_WS_VENV/bin/python3" "$E2E_LIB_DIR/ws_client.py" "$@" \
		>"$out" 2>"$err" || true
}

_ws_parse_status() {
	# $1 = stderr file path. Sets $WS_HANDSHAKE_STATUS / $WS_CLOSE_CODE.
	#
	# Both grep pipelines are wrapped in `|| true` because `set -o
	# pipefail` is in effect (via common.sh) and CLOSE_CODE is normally
	# absent from handshake-only runs — without the guard, a no-match
	# grep aborts the calling test before assert_equals ever runs.
	WS_HANDSHAKE_STATUS=""
	WS_CLOSE_CODE=""
	if [ -s "$1" ]; then
		WS_HANDSHAKE_STATUS=$(grep '^HANDSHAKE_STATUS:' "$1" 2>/dev/null | head -1 | awk '{print $2}' || true)
		WS_CLOSE_CODE=$(grep '^CLOSE_CODE:' "$1" 2>/dev/null | head -1 | awk '{print $2}' || true)
	fi
	export WS_HANDSHAKE_STATUS WS_CLOSE_CODE
}

# Handshake-only check. After this returns, $WS_HANDSHAKE_STATUS holds
# the HTTP status code observed on the upgrade response (101 = accepted,
# 403 = rejected by the server).
ws_handshake() {
	local url="$1"
	shift
	local out err
	out=$(mktemp)
	err=$(mktemp)
	_ws_run "$out" "$err" "$url" --max-messages 0 --timeout 5 "$@"
	_ws_parse_status "$err"
	rm -f "$out" "$err"
}

# Synchronous read. After this returns, $WS_OUT is a temp file with
# newline-delimited JSON messages, $WS_HANDSHAKE_STATUS / $WS_CLOSE_CODE
# are populated. Caller is responsible for `rm -f "$WS_OUT"` if it
# wants to clean up.
ws_read() {
	local url="$1"
	shift
	WS_OUT=$(mktemp /tmp/e2e-ws-out.XXXXXX)
	WS_ERR=$(mktemp /tmp/e2e-ws-err.XXXXXX)
	_ws_run "$WS_OUT" "$WS_ERR" "$url" "$@"
	_ws_parse_status "$WS_ERR"
	export WS_OUT WS_ERR
}

# Background read. Returns immediately with $WS_PID set; messages
# accumulate in $WS_OUT until the client exits (timeout or
# --max-messages reached) or ws_wait kills it.
ws_start_background() {
	local url="$1"
	shift
	ws_ensure_client
	WS_OUT=$(mktemp /tmp/e2e-ws-out.XXXXXX)
	WS_ERR=$(mktemp /tmp/e2e-ws-err.XXXXXX)
	"$E2E_WS_VENV/bin/python3" "$E2E_LIB_DIR/ws_client.py" "$url" "$@" \
		>"$WS_OUT" 2>"$WS_ERR" &
	WS_PID=$!
	export WS_OUT WS_ERR WS_PID
}

# Wait for a backgrounded WS client. $1 is the max seconds to wait
# before killing. Parses status from $WS_ERR on completion.
ws_wait() {
	local timeout="${1:-15}"
	local elapsed=0
	while kill -0 "$WS_PID" 2>/dev/null; do
		if [ "$elapsed" -ge "$timeout" ]; then
			kill "$WS_PID" 2>/dev/null || true
			break
		fi
		sleep 1
		elapsed=$((elapsed + 1))
	done
	wait "$WS_PID" 2>/dev/null || true
	_ws_parse_status "$WS_ERR"
}
