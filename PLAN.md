# Plan: Logs Viewer on Container Detail Page

Add a logs section to `/views/containers/:id` that lets operators browse
micro-container logs (live Docker stream and captured files) and orchestrator
logs filtered to that container, with the selected source persisted as a URL
query parameter.

---

## Backend — New orchestrator endpoint for filtered orchestrator logs

The orchestrator already proxies Docker logs for micro-containers via
`DockerClient.get_container_logs()`. We use the exact same mechanism to fetch
the orchestrator container's own logs and filter them by `container_id`.
No file-writing, no log handlers, no rotation — just a Docker API call at
request time.

- [ ] **Auto-detect the orchestrator's own Docker container ID at startup.**
  In `lifespan()` (in `orchestrator/app.py`), after the Docker client is
  initialised, read `/proc/self/cgroup` and extract the 64-character hex
  container ID. Store it in `app.state.orchestrator_docker_id` (or `None` if
  detection fails). This is zero-config for standard Docker deployments.

  ```python
  import re

  def _detect_own_container_id() -> str | None:
      try:
          text = Path("/proc/self/cgroup").read_text()
          for line in text.splitlines():
              m = re.search(r'/([a-f0-9]{64})$', line)
              if m:
                  return m.group(1)
      except OSError:
          pass
      return None
  ```

  > If detection fails (uncommon — could happen in non-Docker runtimes or
  > certain cgroupv2 setups), the endpoint below returns a 503 with a clear
  > message rather than silently returning empty data.

- [ ] **Add `GET /{container_id}/logs/orchestrator` to
  `orchestrator/routers/containers.py`.**
  This slots neatly into the existing log-endpoint hierarchy:

  | Endpoint | Source |
  |---|---|
  | `/{id}/logs` | Live Docker logs for the micro-container |
  | `/{id}/logs/files` | List of Drover-captured files |
  | `/{id}/logs/files/{filename}` | A specific captured file |
  | `/{id}/logs/orchestrator` | Orchestrator Docker logs, filtered by `{id}` |

  Implementation:
  - Validate the container exists with `_ensure_container_exists` (same
    pattern as the other log endpoints).
  - Read `request.app.state.orchestrator_docker_id`. If `None`, return
    `503` with detail `"Orchestrator container ID could not be detected"`.
  - Call `docker.get_container_logs(orchestrator_docker_id, tail="all")` —
    the same call used for micro-containers.
  - Filter the returned text line-by-line, keeping only lines that contain
    `container_id` as a substring. The 26-character ULID format makes
    false-positive matches negligible.
  - Return the filtered content as `PlainTextResponse`.

  ```python
  @router.get("/{container_id}/logs/orchestrator")
  async def get_orchestrator_logs(
      container_id: str, request: Request
  ) -> PlainTextResponse:
      await _ensure_container_exists(request, container_id)
      own_id = request.app.state.orchestrator_docker_id
      if own_id is None:
          raise HTTPException(
              status_code=503,
              detail="Orchestrator container ID could not be detected",
          )
      docker = request.app.state.docker
      body = await docker.get_container_logs(own_id, tail="all")
      lines = [l for l in body.splitlines(keepends=True) if container_id in l]
      return PlainTextResponse(content="".join(lines))
  ```

  > **Note:** Docker's log buffer is finite (controlled by the host daemon's
  > log driver settings). Logs older than what Docker retains won't appear.
  > This is acceptable since we explicitly don't need persistence across
  > orchestrator restarts.

---

## Frontend — Webapp route changes (`webapp/src/routes/views.js`)

The container detail route (`GET /containers/:id`) currently fetches only the
container record. Extend it to also gather everything needed to render the logs
section.

- [ ] **Read the `log_source` query parameter** from `req.query.log_source`.
  Valid values:
  - `"live"` (or absent/unknown) → fetch from `/{id}/logs`
  - `"file:{filename}"` → fetch from `/{id}/logs/files/{filename}` after
    validating the filename is in the files list (prevents path traversal)
  - `"orchestrator"` → fetch from `/{id}/logs/orchestrator`

- [ ] **Fetch the file list** from `/{id}/logs/files` in parallel with the
  container fetch.
  - On **409**: treat as `filesUnavailable: true` (DROVER_LOG_DIR not set);
    set `logFiles = []`.
  - On any other error: propagate as usual (existing error handling).

- [ ] **Fetch the selected log content** based on the resolved `log_source`.
  - Default to `"live"` when no param or an unrecognised value is provided.
  - When `log_source` is `"file:{filename}"` and the filename is **not** in the
    file list returned above, fall back to `"live"` silently (guards against
    stale URLs after log rotation).
  - Capture the raw text string as `logContent`.
  - On **409** or **503** from any log endpoint: set `logContent = null` and
    `logUnavailable: true` (so the viewer can show an appropriate message).
  - On **404** from the live logs endpoint (container has no Docker ID yet, or
    was never started): set `logContent = ""` so the viewer renders as empty
    rather than showing an error.

- [ ] **Pass all log state to the view:**
  ```js
  containerDetailPage(container, {
    logFiles,          // string[]
    filesUnavailable,  // bool — DROVER_LOG_DIR not set
    logSource,         // resolved string ("live" | "file:…" | "orchestrator")
    logContent,        // string | null (null means unavailable/error)
  })
  ```

---

## Frontend — View template changes (`webapp/src/views/partials/container-detail.js`)

- [ ] **Add a `logsSection(id, opts)` function** that renders below the action
  bar. The section contains:

  1. A `<select>` element with an `onchange` handler that navigates to the same
     page with the updated query parameter:
     ```js
     onchange="window.location.href='/views/containers/${id}?log_source='+this.value"
     ```
     The currently-selected `logSource` is marked as `selected` on the
     matching `<option>`.

  2. **Dropdown options:**
     - `<option value="live">Live container logs</option>` — always present,
       this is the default
     - One `<option value="file:{filename}">{filename}</option>` for each
       entry in `logFiles`
     - `<option value="orchestrator">Orchestrator logs</option>` — always
       present

  3. If `filesUnavailable` is true, show a small note beneath the select:
     > "File-based log capture is not configured (DROVER_LOG_DIR is unset)"

  4. A `<pre id="log-viewer">` block that renders `logContent`:
     - If `logContent` is `null` (unavailable): render a short message instead
       of the `<pre>`:
       > "Container logging not configured"
     - If `logContent` is `""`: render `(no log output)` in a muted style.
     - Otherwise: render `logContent` verbatim inside `<pre>`.

- [ ] **Wire the section into `containerDetailPage()`** after `actionBar()`:
  ```js
  ${logsSection(container.id, logOpts)}
  ```

---

## Rough edges and explicit decisions

- [ ] **Log content size / truncation:** For now display the full content of
  whatever is fetched. The `/{id}/logs` endpoint already supports a `?tail=N`
  query param — expose that later when needed. Orchestrator logs are filtered
  to one container so they're naturally small.

- [ ] **No auto-refresh:** The page is fully server-rendered on load. The
  `onchange` navigation reloads the page with the new selection. This is
  intentional — auto-refresh is deferred until WebSocket support lands.

- [ ] **URL encoding of filenames in query params:** When building the
  `log_source=file:{filename}` value in the `<option>` element, and when
  parsing it in the route, use `encodeURIComponent` / `decodeURIComponent`
  around the filename part so filenames containing special characters (e.g.
  colons in ISO timestamps) round-trip cleanly.

- [ ] **Destroying/destroyed containers:** Live Docker logs (`/logs`) return
  404 after the container is removed from Docker. The route falls back to
  empty content (see 404 handling above). The captured files remain on disk
  until explicitly discarded, so file options still work.

- [ ] **Security — path traversal:** The filename in `log_source=file:{…}`
  must be validated against the list returned by `/logs/files` on the server
  side before being forwarded to the orchestrator. This is already called out
  in the route step above.

- [ ] **Orchestrator container ID detection reliability:** `/proc/self/cgroup`
  works in all standard Docker deployments (both cgroupv1 and cgroupv2). If an
  operator runs in an unusual environment where detection fails, they'll see a
  503 on the orchestrator-logs option and can file an issue. A
  `DROVER_ORCHESTRATOR_CONTAINER_ID` override env var could be added as a
  follow-up if this proves problematic in practice.

- [ ] **`orchestrator.log` / container log directories don't apply here:**
  The simplified approach uses no files on disk for orchestrator logs, so
  there is no naming collision to worry about with the `{log_dir}/{container_id}/`
  directory structure.
