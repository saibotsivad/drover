#!/usr/bin/env bash
# Thin curl wrappers used by the e2e test scripts.
#
# Every function:
# - Sends the bearer token from $DROVER_API_KEY when the var is non-empty.
# - Captures the response body in $E2E_RESPONSE_BODY and the HTTP status
#   code in $E2E_RESPONSE_STATUS so the caller can inspect both without
#   re-curling.
# - Records the last request in $E2E_LAST_REQUEST_METHOD /
#   $E2E_LAST_REQUEST_URL / $E2E_LAST_REQUEST_BODY so step_end can include
#   the request payload in the chunk file.

# Internal: do the HTTP call, populate response globals.
_e2e_curl() {
	local method="$1" url="$2" body="${3:-}"

	E2E_LAST_REQUEST_METHOD="$method"
	E2E_LAST_REQUEST_URL="$url"
	E2E_LAST_REQUEST_BODY="$body"

	local args=(-sS -o /tmp/e2e-resp-body.$$ -w '%{http_code}' -X "$method" "$url")
	if [ -n "${DROVER_API_KEY:-}" ]; then
		args+=(-H "Authorization: Bearer $DROVER_API_KEY")
	fi
	if [ -n "$body" ]; then
		args+=(-H 'Content-Type: application/json' -d "$body")
	fi

	# Don't pass -f: we want the body even on 4xx/5xx so callers can show it.
	local status
	if ! status=$(curl "${args[@]}" 2>/tmp/e2e-resp-err.$$); then
		E2E_RESPONSE_STATUS="000"
		E2E_RESPONSE_BODY=$(cat /tmp/e2e-resp-err.$$ 2>/dev/null || true)
		rm -f /tmp/e2e-resp-body.$$ /tmp/e2e-resp-err.$$
		return 1
	fi

	E2E_RESPONSE_STATUS="$status"
	E2E_RESPONSE_BODY=$(cat /tmp/e2e-resp-body.$$ 2>/dev/null || true)
	rm -f /tmp/e2e-resp-body.$$ /tmp/e2e-resp-err.$$
	return 0
}

api_get() {
	_e2e_curl GET "$1"
}

api_post() {
	_e2e_curl POST "$1" "${2:-}"
}

api_delete() {
	_e2e_curl DELETE "$1"
}

# Wait until $url returns HTTP 200 (with auth header attached), or fail
# after $timeout seconds. Polls every second.
wait_healthy() {
	local url="$1" timeout="${2:-60}"
	local i=0
	while [ "$i" -lt "$timeout" ]; do
		if _e2e_curl GET "$url" && [ "$E2E_RESPONSE_STATUS" = "200" ]; then
			echo "  $url healthy after ${i}s"
			return 0
		fi
		i=$((i + 1))
		sleep 1
	done
	echo "  $url did not become healthy within ${timeout}s (last status=$E2E_RESPONSE_STATUS)" >&2
	return 1
}

# Poll GET /containers/$id until status == $target, the container errors
# out, or the timeout (in seconds) expires. On success, $E2E_RESPONSE_BODY
# holds the final container JSON. The number of polls is recorded in
# $E2E_LAST_POLL_COUNT and the elapsed wall-clock ms in
# $E2E_LAST_POLL_ELAPSED_MS so step_end can include them in the chunk.
wait_container_status() {
	local container_id="$1" target="$2" timeout="${3:-30}"
	local start_ms
	start_ms=$(date +%s%3N)
	local polls=0
	local i=0
	while [ "$i" -lt "$timeout" ]; do
		polls=$((polls + 1))
		if api_get "${ORCHESTRATOR_URL}/containers/${container_id}"; then
			local status
			status=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status' 2>/dev/null || echo "")
			if [ "$status" = "$target" ]; then
				E2E_LAST_POLL_COUNT="$polls"
				E2E_LAST_POLL_ELAPSED_MS=$(( $(date +%s%3N) - start_ms ))
				return 0
			fi
			if [ "$status" = "error" ] && [ "$target" != "error" ]; then
				E2E_LAST_POLL_COUNT="$polls"
				E2E_LAST_POLL_ELAPSED_MS=$(( $(date +%s%3N) - start_ms ))
				echo "  container $container_id transitioned to 'error' while waiting for '$target'" >&2
				return 1
			fi
		fi
		i=$((i + 1))
		sleep 1
	done
	E2E_LAST_POLL_COUNT="$polls"
	E2E_LAST_POLL_ELAPSED_MS=$(( $(date +%s%3N) - start_ms ))
	echo "  container $container_id did not reach status '$target' within ${timeout}s" >&2
	return 1
}

# Poll GET /containers/$cid/exec/$cmd_id until status == "complete". Echoes
# the final exec JSON to stdout (so callers can pipe to jq) and also leaves
# it in $E2E_RESPONSE_BODY.
wait_exec_complete() {
	local container_id="$1" command_id="$2" timeout="${3:-30}"
	local start_ms
	start_ms=$(date +%s%3N)
	local polls=0
	local i=0
	while [ "$i" -lt "$timeout" ]; do
		polls=$((polls + 1))
		if api_get "${ORCHESTRATOR_URL}/containers/${container_id}/exec/${command_id}"; then
			local status
			status=$(printf '%s' "$E2E_RESPONSE_BODY" | jq -r '.status' 2>/dev/null || echo "")
			if [ "$status" = "complete" ]; then
				E2E_LAST_POLL_COUNT="$polls"
				E2E_LAST_POLL_ELAPSED_MS=$(( $(date +%s%3N) - start_ms ))
				printf '%s' "$E2E_RESPONSE_BODY"
				return 0
			fi
		fi
		i=$((i + 1))
		sleep 1
	done
	E2E_LAST_POLL_COUNT="$polls"
	E2E_LAST_POLL_ELAPSED_MS=$(( $(date +%s%3N) - start_ms ))
	echo "  exec $command_id on $container_id did not complete within ${timeout}s" >&2
	return 1
}
