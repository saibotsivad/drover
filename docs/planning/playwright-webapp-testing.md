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

### Selector conventions

All element selectors follow `docs/test-selector-conventions.md` — stable `id` and
`class` attributes on the HTML, no `data-testid`. The quick reference used in these
tests:

| What | Selector |
|------|----------|
| Page section root | `#images-list`, `#containers-list`, `#container-detail`, `#launch-form`, `#exec-detail` |
| Images table body | `tbody#image-rows` |
| One image row | `#image-{drover.name}` |
| Containers table body | `tbody#container-rows` |
| One container row | `#container-{id}` |
| Status badge | `.status-{slug}` (e.g. `.status-running`, `.status-complete`) |
| Exec input form | `.exec-input-form` |
| Exec command textarea | `textarea[name="command"]` within `.exec-input-form` |
| Exec submit button | `button[type="submit"]` within `.exec-input-form` |
| Command rows tbody | `tbody#command-rows` |
| One command row | `#command-{commandId}` |
| Exec output content | `pre#exec-output` |
| Log source picker | `select.log-source-select` |
| Log content pane | `pre#log-viewer` |
| Stop button | `.btn-stop` |
| Destroy button | `.btn-destroy` |

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

1. Call `GET /images` on the orchestrator (via `page.request`) and collect the
   `name` values from the JSON response.
2. Navigate to `/views/images`.
3. For each name from step 1, assert that `#image-{name}` exists within `#images-list`.
4. Assert that `tbody#image-rows tr` count equals the number of images from the API.

In the e2e stack the only image with `drover.managed=true` is `builder`, so
`#image-builder` should exist and the row count should be exactly 1.

---

### Test 2 — Launch privileged container, exec echo, verify output (`privileged-launch.spec.ts`)

**What it covers:** The launch form submits correctly, the exec input form runs a
command, and the exec output page shows the expected result.

**Steps:**

1. Navigate to `/views/launch`.
2. Select `builder` from the image dropdown.
3. Check the `privileged` checkbox.
4. Enter `DROVER_TEST_VAR=hello_playwright` in the environment variables textarea.
5. Click **Launch**.
6. Assert the browser redirects to `/views/containers/<id>`.
7. Extract the container ID from the URL — shared across all remaining tests.
8. Poll `GET /containers/{id}` via `page.request` until status is `running` (up to
   30 s), then reload the detail page.
9. Fill `textarea[name="command"]` within `.exec-input-form` with
   `echo $DROVER_TEST_VAR`.
10. Click `button[type="submit"]` within `.exec-input-form`.
11. Wait for htmx to replace `#command-rows` — a non-empty `tr` appears.
12. Extract the command ID from the first `tr` in `#command-rows` (its `id` attribute
    is `command-{commandId}`), then click its link to navigate to
    `/views/containers/{id}/execs/{commandId}`.
13. On the exec output page, reload until `#exec-detail .status-complete` is present
    (up to 15 s — `echo` is near-instant, usually complete on first load).
14. Assert `pre#exec-output` contains `hello_playwright`.
15. Assert the exit code shown in `#exec-meta` is `0`.

---

### Test 3 — Exec `docker container ls`, verify privileged access (`privileged-launch.spec.ts`)

**What it covers:** A privileged container has access to the Docker socket.

Reuses the container from Test 2.

**Steps:**

1. Navigate back to `/views/containers/{id}`.
2. Fill `.exec-input-form textarea[name="command"]` with `docker container ls`.
3. Click the submit button.
4. Wait for the new command row in `#command-rows`, extract the command ID, navigate
   to its exec output page.
5. Reload until `#exec-detail .status-complete` (up to 30 s — Docker CLI startup is
   slower than a shell built-in).
6. Assert `pre#exec-output` contains `CONTAINER ID` (the column header of
   `docker container ls`).
7. Assert exit code in `#exec-meta` is `0`.

---

### Test 4 — Launched container appears in containers list (`privileged-launch.spec.ts`)

**What it covers:** The `/views/containers` list reflects containers created through
the UI.

Reuses the container from Test 2.

**Steps:**

1. Navigate to `/views/containers`.
2. Assert `#container-{id}` exists within `#containers-list tbody#container-rows`.
3. Assert `#container-{id} .status-running` (or `.status-initializing`) exists.

---

### Test 5 — Log viewer sources on container detail page (`privileged-launch.spec.ts`)

**What it covers:** The log source `<select>` renders content for each source type.

Reuses the container from Test 2.

**Steps:**

1. Navigate to `/views/containers/{id}` (default `log_source=live`).
2. Assert `pre#log-viewer` is present and contains `Connecting to /run/orchestrator.sock`.
3. Poll `GET /containers/{id}/logs/files` via `page.request` until the response
   includes `0.log` (up to 15 s), then reload the detail page.
4. Call `select.log-source-select.selectOption('file:0.log')` — triggers `onchange`
   navigation to `?log_source=file%3A0.log`.
5. Assert `pre#log-viewer` contains `Connecting to`.
6. Call `select.log-source-select.selectOption('orchestrator')` — triggers navigation
   to `?log_source=orchestrator`.
7. Assert `pre#log-viewer` contains the container ID.

---

## Implementation checklist

- [ ] Add `playwright-runner` service with image `mcr.microsoft.com/playwright:v1.52.0-noble`
      to `docker-compose.e2e.yml` (profiles: playwright).
- [ ] Create `e2e/playwright/package.json` with `@playwright/test` dependency.
- [ ] Create `e2e/playwright/playwright.config.ts` (single Chromium project, env-var
      `baseURL`, output to `results/`).
- [ ] Write `e2e/playwright/tests/images-list.spec.ts` (Test 1).
- [ ] Write `e2e/playwright/tests/privileged-launch.spec.ts` (Tests 2–5, shared
      container fixture; covers exec form, exec output page, containers list, and
      log viewer).
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

- **Exec UI is fully implemented:** The container detail page has an exec input form
  (`.exec-input-form`) and a command list (`tbody#command-rows`). Each command links
  to a dedicated exec output page (`#exec-detail` / `pre#exec-output`). Tests 2 and
  3 exercise these flows end-to-end, restoring the original test intent.
