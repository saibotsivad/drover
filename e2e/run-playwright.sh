#!/usr/bin/env bash
# Playwright browser-test entry point.
#
# Runs the Playwright suite against an already-up Drover stack (the same
# stack that `./e2e/run.sh up` brings up). Tests live in `e2e/playwright/`
# and execute inside the `playwright-runner` service from
# `docker-compose.e2e.yml`, behind the `playwright` profile so they never
# start during a normal `compose up`.
#
# Usage:
#   ./e2e/run-playwright.sh    Run all Playwright tests (stack must be up)
#
# The HTML report and any trace archives land in `e2e/playwright-results/`.
# That directory sits alongside `e2e/logs/` rather than inside it so the
# Drover service logs (what the stack did) stay clearly separated from
# the test-runner artifacts (what the tests saw).

set -Eeuo pipefail

E2E_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMPOSE_FILE="$E2E_DIR/docker-compose.e2e.yml"

set -a
# shellcheck disable=SC1091
. "$E2E_DIR/.env.test"
set +a

compose() {
	docker compose -f "$COMPOSE_FILE" "$@"
}

echo "==> Verifying webapp is reachable at ${WEBAPP_URL}"
if ! curl -sf "${WEBAPP_URL}/health" >/dev/null 2>&1; then
	echo "Webapp is not reachable at ${WEBAPP_URL} — did you run './e2e/run.sh up'?" >&2
	exit 1
fi

# Ensure the bind-mount target exists with permissions the playwright-runner
# container (which runs as a non-root user) can write to.
mkdir -p "$E2E_DIR/playwright-results"
chmod 777 "$E2E_DIR/playwright-results" || true

echo "==> Running Playwright suite"
status=0
compose --profile playwright run --rm playwright-runner || status=$?

if [ "$status" -eq 0 ]; then
	echo "==> Playwright tests passed."
else
	echo "==> Playwright tests failed (exit $status)." >&2
	echo "==> See e2e/playwright-results/ for the HTML report and traces." >&2
fi

exit "$status"
