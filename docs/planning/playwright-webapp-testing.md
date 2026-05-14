# Playwright Webapp Testing

The current E2E suite (see [`docs/full-e2e-suite.md`](../full-e2e-suite.md)) validates
the webapp's `/health` endpoint via curl but does not exercise the webapp UI. This
document tracks the plan to add browser-level tests using Playwright.

## Goal

Add `e2e/playwright/` alongside the existing bash test suite to test webapp
functionality end-to-end in a real browser. These tests run against the same stack
that `e2e/run.sh up` brings up and exercise the UI flows that the bash tests cannot
reach.

## How Playwright fits in

Playwright tests live in `e2e/playwright/` and are invoked by a new
`e2e/run-playwright.sh` script so they can be run or skipped independently of the
bash suite. The `run.sh ci` command will call `run-playwright.sh` after `cmd_test`
completes, keeping the overall ci flow as a single entry point.

The bash tests and Playwright tests share the same already-running stack; no changes
to `docker-compose.e2e.yml` or the existing test scripts are needed.

## Playwright execution environment

Playwright and its Chromium browser will run inside a dedicated Docker container
(`playwright-runner`) defined in `docker-compose.e2e.yml`. This keeps the host
environment clean and makes the suite portable across CI and developer machines.

### New service in `docker-compose.e2e.yml`

```yaml
playwright-runner:
  image: mcr.microsoft.com/playwright:v1.52.0-noble
  working_dir: /tests
  volumes:
    - ./playwright:/tests          # test source
    - ./logs:/tests/results        # HTML report + traces go here
  environment:
    WEBAPP_URL: http://webapp:9091
    ORCHESTRATOR_URL: http://orchestrator:8000
    DROVER_API_KEY: drover-e2e-test-key
  depends_on:
    - webapp
    - orchestrator
  network_mode: service:webapp     # shares the compose network so WEBAPP_URL resolves
  command: ["npx", "playwright", "test", "--reporter=list,html"]
  profiles: ["playwright"]         # not started by `compose up`; run explicitly
```

Using the official `mcr.microsoft.com/playwright` image means Chromium and all
native dependencies are pre-installed — no manual browser install step needed.

The `profiles: ["playwright"]` entry means normal `compose up` and `compose down`
calls leave it alone. `run-playwright.sh` will run it with
`docker compose --profile playwright run --rm playwright-runner`.

### `e2e/playwright/` directory layout

```
e2e/playwright/
  package.json          # playwright dependency and test script
  playwright.config.ts  # single Chromium project, baseURL from env
  tests/
    images-list.spec.ts       # test 1
    privileged-launch.spec.ts # tests 2–5
```

### `e2e/run-playwright.sh`

A thin wrapper that:
1. Checks that the stack is up (curl `$WEBAPP_URL/health`).
2. Runs `docker compose -f e2e/docker-compose.e2e.yml --profile playwright run --rm playwright-runner`.
3. Exits with the container's exit code so CI can pick it up.

Playwright's HTML report and trace archives land in `e2e/logs/playwright/` (via the
volume mount) and get picked up by the same CI artifact upload step that already
captures `e2e/logs/`.

## Tests to implement

### Test 1 — Images page matches orchestrator API (`images-list.spec.ts`)

**What it covers:** The `/views/images` page renders the correct list of images.

**Steps:**

1. Call `GET http://orchestrator:8000/images` with the test API key and collect the
   `drover.name` values from the JSON response.
2. Navigate to `http://webapp:9091/views/images`.
3. Assert that every `drover.name` value from step 1 appears in the rendered table
   (match on the name column text).
4. Assert the row count in the table equals the number of images returned by the API.

In the e2e stack the only image with `drover.managed=true` is `builder`, so the
table should have exactly one row.

---

### Test 2 — Launch privileged container and verify live log viewer (`privileged-launch.spec.ts`)

**What it covers:** The launch form submits correctly, the container detail page
loads, and the live log viewer shows the executor's startup output.

There is no exec form in the webapp UI — exec is API-only and is fully covered by
the bash tests. The Playwright tests focus on the log viewer that landed with the
logs feature.

**Steps:**

1. Navigate to `/views/launch`.
2. Select `builder` from the image dropdown.
3. Check the `privileged` checkbox.
4. Enter `DROVER_TEST_VAR=hello_playwright` in the environment variables textarea.
5. Click **Launch**.
6. Assert the browser redirects to `/views/containers/<id>` (detail page).
7. Extract the container ID from the URL — this is shared across Tests 3 and 4.
8. Poll the orchestrator API via Playwright's `request` context until the container
   status is `running` (up to 30 s).
9. Reload the detail page (or navigate to it fresh after the poll resolves).
10. Assert that `select.log-source-select` is present.
11. Assert that `pre#log-viewer` is present and its text content is non-empty. The
    executor logs `Connecting to /run/orchestrator.sock` on startup; that line is a
    reliable signal that live logs are flowing.

> Note: step 8 uses `page.request.get(...)` against the orchestrator URL (exposed
> at `ORCHESTRATOR_URL` env var) so the test does not busy-loop by reloading the
> page. The live log endpoint is a plain text GET with no streaming; a single page
> load after the poll resolves is sufficient.

---

### Test 3 — Switch to file-based log source (`privileged-launch.spec.ts`)

**What it covers:** The log source `<select>` navigates to the correct URL and the
file log viewer renders captured content.

This test reuses the container from Test 2 via the shared `beforeAll` fixture.

The e2e stack sets `DROVER_LOG_DIR=/var/lib/orchestrator/logs`, so file capture is
active. The orchestrator starts writing `0.log` as soon as the executor connects,
which happens before the container reaches `running`. By the time Test 3 runs, the
file should be present.

**Steps:**

1. Still on the container detail page from Test 2.
2. Poll `GET /containers/{id}/logs/files` via `page.request` until the response
   includes `0.log` (up to 15 s).
3. Select the `0.log` option from `select.log-source-select`.
4. Assert the page URL includes `log_source=file%3A0.log` (or the equivalent
   `encodeURIComponent` form) after the `onchange` navigation.
5. Assert `pre#log-viewer` is present and contains `Connecting to`.

---

### Test 4 — Switch to orchestrator log source (`privileged-launch.spec.ts`)

**What it covers:** Selecting "Orchestrator logs" from the dropdown navigates
correctly and renders content that is scoped to this container.

This test also reuses the container from Test 2.

**Steps:**

1. On the container detail page, select "Orchestrator logs" from
   `select.log-source-select`.
2. Assert the page URL includes `log_source=orchestrator`.
3. Assert `pre#log-viewer` is present and its text contains the container ID.

---

### Test 5 — Launched container appears in containers list (`privileged-launch.spec.ts`)

**What it covers:** The `/views/containers` list reflects containers created through
the UI.

This test also reuses the container from Test 2.

**Steps:**

1. Navigate to `/views/containers`.
2. Assert that a row containing the container ID from Test 2 exists in the table.
3. Assert that the status badge for that row shows an active status.

---

## Implementation checklist

- [ ] Add `playwright-runner` service with image `mcr.microsoft.com/playwright:v1.52.0-noble`
      to `docker-compose.e2e.yml` (profiles: playwright).
- [ ] Create `e2e/playwright/package.json` with `@playwright/test` dependency.
- [ ] Create `e2e/playwright/playwright.config.ts` (single Chromium project, env-var
      `baseURL`, output to `results/`).
- [ ] Write `e2e/playwright/tests/images-list.spec.ts` (Test 1).
- [ ] Write `e2e/playwright/tests/privileged-launch.spec.ts` (Tests 2–5, shared
      container fixture).
- [ ] Write `e2e/run-playwright.sh` (health check → compose run → exit-code passthrough).
- [ ] Update `run.sh ci` to call `run-playwright.sh` after `cmd_test`.
- [ ] Verify Playwright HTML report lands in `e2e/logs/playwright/` and is included
      in the existing CI log-upload artifact.
- [ ] Add a note to `e2e/README.md` explaining how to run the Playwright suite
      locally.

## Settled decisions

- **Shared container for Tests 2–5:** Tests 2 through 5 share one container via a
  `test.describe`-level `beforeAll` fixture. This matches how the existing bash suite
  works within a single test file.

- **Playwright image tag:** Pinned to `mcr.microsoft.com/playwright:v1.52.0-noble`.
  Bump deliberately; never use `latest`.

- **No test teardown:** Tests 2–5 leave the container running. The `e2e/run.sh down`
  step already cleans up all `drover.managed=true` containers, so no `afterAll` stop
  hook is needed.

- **No exec UI, no exec tests in Playwright:** The container detail page has no exec
  form. Exec is API-only; it is fully covered by the bash tests (03- and 05-). The
  Playwright tests focus on the log viewer instead, which is the only UI surface that
  overlaps with container runtime behavior.
