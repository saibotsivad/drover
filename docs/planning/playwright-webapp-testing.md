# Playwright Webapp Testing

The current E2E suite (see [`docs/full-e2e-suite.md`](../full-e2e-suite.md)) validates
the webapp's `/health` endpoint via curl but does not exercise the webapp UI. This
document tracks the plan to add browser-level tests using Playwright.

## Goal

Add `e2e/playwright/` alongside the existing bash test suite to test webapp
functionality end-to-end in a real browser. These tests would run against the same
stack that `e2e/run.sh up` brings up, exercising the UI flows that the bash tests
cannot reach.

## How Playwright fits in

Playwright tests live in `e2e/playwright/` and run independently of the bash suite.
Triggering options to decide during implementation:

- A new `run.sh test --browser` flag that runs both bash and Playwright tests.
- A separate `e2e/run-playwright.sh` script so the browser suite can be run or skipped
  independently.

Either way, adding Playwright does not require restructuring the existing bash tests or
the stack definition in `e2e/docker-compose.e2e.yml`.

## What to test

At minimum, the Playwright suite should cover the core webapp flows: whatever a user
can do through the UI that isn't already covered by the API-level bash tests. The exact
scope is TBD once the webapp UI matures.
