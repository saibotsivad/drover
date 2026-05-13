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
  image: mcr.microsoft.com/playwright:v1.x.x-noble
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
    privileged-launch.spec.ts # tests 2, 3, and 4
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

### Test 2 — Launch privileged container with env var, exec echo, verify output (`privileged-launch.spec.ts`)

**What it covers:** The launch form submits correctly and the resulting container
detail page shows exec output.

**Steps:**

1. Navigate to `/views/launch`.
2. Select `builder` from the image dropdown.
3. Check the `privileged` checkbox.
4. Enter `DROVER_TEST_VAR=hello_playwright` in the environment variables textarea.
5. Click **Launch**.
6. Assert the browser redirects to `/views/containers/<id>` (detail page).
7. On the detail page, locate the exec form and submit the command
   `echo $DROVER_TEST_VAR`.
8. Wait for the output panel to appear (the page polls via htmx).
9. Assert that the output panel contains `hello_playwright`.
10. Assert exit code is `0`.

> Note: the exec flow on the detail page is htmx-driven polling; the test will need
> a reasonable `waitFor` / `locator.waitFor` timeout (suggest 30 s) to allow the
> container to reach `running` and the exec to complete.

---

### Test 3 — Privileged container can see host Docker (`privileged-launch.spec.ts`)

**What it covers:** A privileged container actually has access to the Docker socket.

This test reuses the container launched in Test 2 (use Playwright's
`test.describe`-level fixture to share state within the file).

**Steps:**

1. On the detail page for the same container from Test 2, submit the command
   `docker container ls`.
2. Wait for the output panel.
3. Assert the command exits with code `0`.
4. Assert the output contains `CONTAINER ID` (the header row of `docker container ls`).

---

### Test 4 — Launched container appears in containers list (`privileged-launch.spec.ts`)

**What it covers:** The `/views/containers` list reflects containers created through
the UI.

This test also reuses the container from Test 2.

**Steps:**

1. Navigate to `/views/containers`.
2. Assert that a row with the container ID (or name/label) from Test 2 exists in
   the table.
3. Assert that the status badge for that row shows `running` (or any active status).

---

## Implementation checklist

- [ ] Pin a specific `mcr.microsoft.com/playwright` image tag (match Node 22 since
      the webapp already uses it) and record it here.
- [ ] Add `playwright-runner` service to `docker-compose.e2e.yml` (profiles:
      playwright).
- [ ] Create `e2e/playwright/package.json` with `@playwright/test` dependency.
- [ ] Create `e2e/playwright/playwright.config.ts` (single Chromium project, env-var
      `baseURL`, output to `results/`).
- [ ] Write `e2e/playwright/tests/images-list.spec.ts` (Test 1).
- [ ] Write `e2e/playwright/tests/privileged-launch.spec.ts` (Tests 2–4, shared
      container fixture).
- [ ] Write `e2e/run-playwright.sh` (health check → compose run → exit-code passthrough).
- [ ] Update `run.sh ci` to call `run-playwright.sh` after `cmd_test`.
- [ ] Verify Playwright HTML report lands in `e2e/logs/playwright/` and is included
      in the existing CI log-upload artifact.
- [ ] Add a note to `e2e/README.md` explaining how to run the Playwright suite
      locally.

## Open questions / decisions for team review

- **Shared vs. isolated container for Tests 2–4:** Sharing one container across all
  three tests is faster and mirrors real usage, but a failure in Test 2 will skip
  Tests 3 and 4. Alternatively each test launches its own container (slower but
  independent). Recommendation: share, since the existing bash suite already does
  this within a single test file.

- **Playwright image tag:** `mcr.microsoft.com/playwright:v1.52.0-noble` is current
  as of the time this doc was written. We should pin a tag and bump it deliberately
  rather than using `latest`.

- **Test teardown:** Tests 2–4 leave the container in `running` state. The
  `e2e/run.sh down` step's micro-container cleanup (filter `drover.managed=true`)
  already removes these, so no explicit stop-in-test teardown is strictly required.
  However, it may be cleaner to stop the container in an `afterAll` hook so partial
  runs don't leave debris when `down` is not called.

- **exec UI flow:** The current webapp detail page (`/views/containers/:id`) will
  need to be confirmed to have an exec form and an output panel that Playwright can
  interact with. If that UI is incomplete, Tests 2–4 may need to fall back to calling
  the orchestrator API directly from within the Playwright test for the exec step and
  only assert the containers-list row for Test 4.
