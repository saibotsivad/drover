#!/usr/bin/env bash
# Single entry point for the Drover end-to-end test suite.
#
# Usage:
#   ./e2e/run.sh up                    Build images, start stack, wait healthy
#   ./e2e/run.sh down                  Stop stack, drop volumes
#   ./e2e/run.sh restart               Rebuild + restart all services
#   ./e2e/run.sh restart <service>     Rebuild + restart just one service
#   ./e2e/run.sh test                  Run tests against an already-up stack
#   ./e2e/run.sh ci                    up + test + down (one-shot)
#
# See docs/rfc/2026-05-12-e2e-testing.md for the lifecycle rationale.

set -Eeuo pipefail

E2E_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$E2E_DIR/.." && pwd)
COMPOSE_FILE="$E2E_DIR/docker-compose.e2e.yml"

# Load deterministic test env into this shell for `up` (so SOCKET_DIR is
# created with the right path) and for `test` (so the helpers know which
# endpoints to hit).
set -a
# shellcheck disable=SC1091
. "$E2E_DIR/.env.test"
set +a

# Use `docker compose` (v2) — `docker-compose` (v1) is not assumed.
compose() {
	docker compose -f "$COMPOSE_FILE" "$@"
}

cmd_up() {
	echo "==> Preparing host bind-mount $SOCKET_DIR"
	mkdir -p "$SOCKET_DIR"
	# SOCKET_DIR is the parent that the orchestrator chowns/creates
	# per-container sockets into. The orchestrator runs as a non-root
	# user inside its container but reaches the host filesystem directly
	# via the bind-mount, so the host directory needs to be world-rwx.
	chmod 777 "$SOCKET_DIR" || true

	echo "==> Building images from source"
	compose build

	echo "==> Starting stack"
	compose up -d orchestrator webapp

	echo "==> Waiting for orchestrator health"
	for i in $(seq 1 60); do
		if curl -sf "${ORCHESTRATOR_URL}/health" >/dev/null 2>&1; then
			echo "    orchestrator healthy after ${i}s"
			break
		fi
		if [ "$i" -eq 60 ]; then
			echo "    orchestrator did not become healthy within 60s" >&2
			compose logs orchestrator >&2 || true
			return 1
		fi
		sleep 1
	done

	echo "==> Waiting for webapp health"
	for i in $(seq 1 60); do
		if curl -sf "${WEBAPP_URL}/health" >/dev/null 2>&1; then
			echo "    webapp healthy after ${i}s"
			break
		fi
		if [ "$i" -eq 60 ]; then
			echo "    webapp did not become healthy within 60s" >&2
			compose logs webapp >&2 || true
			return 1
		fi
		sleep 1
	done

	echo "==> Stack is up. Try: ./e2e/run.sh test"
}

cmd_down() {
	echo "==> Stopping stack and dropping volumes"
	compose down --volumes --remove-orphans
	# The bind-mount directory is owned by root after the orchestrator's
	# socket activity. Clean it up so the next `up` starts fresh.
	if [ -d "$SOCKET_DIR" ]; then
		echo "==> Cleaning $SOCKET_DIR"
		# `sudo` may not be available; ignore failures, the next `up`
		# will chmod 777 over the top.
		rm -rf "${SOCKET_DIR:?}/"* 2>/dev/null || true
	fi
}

cmd_restart() {
	local service="${1:-}"
	if [ -z "$service" ]; then
		echo "==> Rebuilding and restarting full stack"
		compose build
		compose up -d --force-recreate orchestrator webapp
	else
		case "$service" in
			orchestrator|webapp|builder) ;;
			*)
				echo "Unknown service '$service' — expected orchestrator | webapp | builder" >&2
				return 2
				;;
		esac
		echo "==> Rebuilding and restarting $service"
		compose build "$service"
		if [ "$service" = "builder" ]; then
			# Builder isn't a long-running service; just rebuild the
			# image. `up` would start a one-shot container that exits.
			return 0
		fi
		compose up -d --force-recreate "$service"
	fi

	# After a restart, wait for the orchestrator (which is what most
	# tests need) to come back. Don't repeat the webapp wait when only
	# the orchestrator was restarted — keep this loose.
	echo "==> Waiting for orchestrator health"
	for i in $(seq 1 60); do
		if curl -sf "${ORCHESTRATOR_URL}/health" >/dev/null 2>&1; then
			echo "    orchestrator healthy after ${i}s"
			return 0
		fi
		sleep 1
	done
	echo "    orchestrator did not become healthy within 60s" >&2
	return 1
}

cmd_test() {
	# Each test invocation gets a fresh run dir under e2e/logs/. The run
	# id is just the ISO timestamp — short enough to type, sortable, and
	# unique unless someone runs the suite twice in the same second.
	local run_id
	run_id=$(date -u +'%Y-%m-%dT%H-%M-%SZ')
	local run_dir="$E2E_DIR/logs/$run_id"
	mkdir -p "$run_dir"

	export E2E_RUN_ID="$run_id"
	export E2E_RUN_LOG_DIR="$run_dir"

	echo "==> Test run id: $run_id"
	echo "==> Logs:        $run_dir"

	# Verify the stack is reachable before sourcing test scripts —
	# otherwise the first test's failure mode is misleading.
	if ! curl -sf "${ORCHESTRATOR_URL}/health" >/dev/null 2>&1; then
		echo "Orchestrator is not reachable at $ORCHESTRATOR_URL — did you run './e2e/run.sh up'?" >&2
		return 1
	fi

	# Initialize summary file with a header so the first chunk's append
	# is the second line.
	{
		printf '# Drover e2e run %s\n' "$run_id"
		printf '# Tests run in lexical order; first FAIL stops the suite.\n'
	} > "$run_dir/summary.log"

	local overall_status=0
	local failed_test=""
	shopt -s nullglob
	local tests=("$E2E_DIR"/tests/*.sh)
	shopt -u nullglob
	# Bash sorts globs lexically by default; the leading two-digit prefix
	# enforces order.
	for test_script in "${tests[@]}"; do
		echo
		echo "================================================================"
		echo "Running $(basename "$test_script")"
		echo "================================================================"
		if bash "$test_script"; then
			:
		else
			overall_status=$?
			failed_test=$(basename "$test_script")
			break
		fi
	done

	echo
	if [ "$overall_status" -eq 0 ]; then
		echo "==> All tests passed."
		printf '# RESULT: PASS\n' >> "$run_dir/summary.log"
	else
		echo "==> Failed: $failed_test (exit $overall_status)" >&2
		printf '# RESULT: FAIL (first failure: %s)\n' "$failed_test" >> "$run_dir/summary.log"
		echo "==> See $run_dir/ for chunked log output." >&2
	fi
	return "$overall_status"
}

cmd_ci() {
	# One-shot run for CI: bring the stack up, run the tests, tear it
	# down. Even if tests fail we still try to tear down — the workflow
	# uploads e2e/logs/ as an artifact before this script exits, so the
	# tear-down failing wouldn't lose anything.
	cmd_up
	local status=0
	cmd_test || status=$?
	cmd_down || true
	return "$status"
}

main() {
	local cmd="${1:-}"
	shift || true
	case "$cmd" in
		up)      cmd_up "$@" ;;
		down)    cmd_down "$@" ;;
		restart) cmd_restart "$@" ;;
		test)    cmd_test "$@" ;;
		ci)      cmd_ci "$@" ;;
		""|-h|--help|help)
			sed -n '2,15p' "$0"
			exit 0
			;;
		*)
			echo "Unknown command: $cmd" >&2
			echo "Try '$0 help'" >&2
			exit 2
			;;
	esac
}

main "$@"
