# Plan: Logs Viewer on Container Detail Page

Add a logs section to `/views/containers/:id` that lets operators browse
micro-container logs (live Docker stream and captured files) and orchestrator
logs filtered to that container, with the selected source persisted as a URL
query parameter.

---

## Backend — New orchestrator endpoint for filtered orchestrator logs

The orchestrator currently logs only to stdout (a `StreamHandler` in
`orchestrator/app.py`). To serve filtered orchestrator logs over HTTP we need
to also write them to a file we can read back.

- [ ] **Extend `setup_logging()` to accept an optional `log_dir` argument.**
  When provided, attach a second `FileHandler` (using the same `_JsonFormatter`)
  that writes to `{log_dir}/orchestrator.log`. Call it from `lifespan()` after
  `load_config()`:
  ```python
  setup_logging(config.log_level, log_dir=config.log_dir)
  ```
  This means the file only appears when `DROVER_LOG_DIR` is set, which is
  consistent with all other log-capture behaviour.

  > **Rotation note:** use `logging.handlers.RotatingFileHandler` with a
  > generous `maxBytes` (e.g. 50 MB) and `backupCount=2` so the file doesn't
  > grow without bound. For the initial implementation a single unrotated file
  > is acceptable if rotation adds complexity — call it out as a follow-up.

- [ ] **Add `GET /containers/{container_id}/orchestrator-logs` to
  `orchestrator/routers/containers.py`.**
  - Validate the container exists with `_ensure_container_exists` (same pattern
    as the other log endpoints).
  - If `config.log_dir` is `None`, raise `HTTPException(409, "LoggingNotEnabled")`
    (same error shape as the existing `LoggingNotEnabled` path in
    `list_log_files` / `get_log_file`).
  - If `{log_dir}/orchestrator.log` does not exist yet (orchestrator just
    started or DROVER_LOG_DIR was set for the first time), return an empty
    `PlainTextResponse` rather than a 404 — the file simply hasn't been
    written yet.
  - Read the file line-by-line and keep only lines that contain `container_id`
    as a substring. Return the filtered content as `PlainTextResponse`.
  - The container ID is a 26-character ULID (`[0-9A-Z]{26}`), so substring
    matching has a negligible false-positive rate. No regex needed.

  ```python
  @router.get("/{container_id}/orchestrator-logs")
  async def get_orchestrator_logs(
      container_id: str, request: Request
  ) -> PlainTextResponse:
      await _ensure_container_exists(request, container_id)
      config: Config = request.app.state.config
      if config.log_dir is None:
          raise HTTPException(status_code=409, detail="LoggingNotEnabled")
      log_path = Path(config.log_dir) / "orchestrator.log"
      if not log_path.exists():
          return PlainTextResponse(content="")
      lines = [l for l in log_path.read_text().splitlines(keepends=True)
               if container_id in l]
      return PlainTextResponse(content="".join(lines))
  ```

  > **Performance note:** for very large `orchestrator.log` files this linear
  > scan is fine initially. If the file grows large, add streaming or an index
  > as a follow-up.

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
  - `"orchestrator"` → fetch from `/{id}/orchestrator-logs`

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
  - On **409** from any log endpoint: set `logContent = null` and
    `logUnavailable: true`.
  - On **404** from the live logs endpoint (container has no Docker ID yet, or
    container was never started): set `logContent = ""` so the viewer renders
    as empty rather than showing an error.

- [ ] **Pass all log state to the view:**
  ```js
  containerDetailPage(container, {
    logFiles,          // string[]
    filesUnavailable,  // bool — DROVER_LOG_DIR not set
    logSource,         // resolved string ("live" | "file:…" | "orchestrator")
    logContent,        // string | null (null means 409)
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
     - If `logContent` is `null` (409): render a short message instead of the
       `<pre>`:
       > "Container logging not configured"
     - If `logContent` is `""`: render `(no log output)` in a muted style.
     - Otherwise: render `logContent` verbatim inside `<pre>`.

- [ ] **Wire the section into `containerDetailPage()`** after `actionBar()`:
  ```js
  ${logsSection(container.id, logOpts)}
  ```

---

## Rough edges and explicit decisions

- [ ] **Log file size / truncation:** For now display the full content of
  whatever is fetched. Add a note in the UI comment that we'll want pagination
  or tail-N once files grow. The `/{id}/logs` endpoint already supports a
  `?tail=N` query param — we can expose that later.

- [ ] **No auto-refresh:** The page is fully server-rendered on load. The
  `onchange` navigation reloads the page with the new selection. This is
  intentional — auto-refresh is deferred until WebSocket support lands.

- [ ] **URL encoding of filenames in query params:** When building the
  `log_source=file:{filename}` value in the `<option>` element, and when
  parsing it in the route, use `encodeURIComponent` / `decodeURIComponent`
  around the filename part so filenames containing special characters (e.g.
  colons in ISO timestamps) round-trip cleanly.

- [ ] **Orchestrator log file not yet written:** The new endpoint returns `""`
  (empty 200) when `orchestrator.log` doesn't exist. The viewer renders this
  as `(no log output)` which is clear and non-alarming.

- [ ] **Destroying/destroyed containers:** Live Docker logs (`/logs`) return
  404 after the container is removed from Docker. The route falls back to
  empty content (see 404 handling above). The captured files remain on disk
  until explicitly discarded, so file options still work.

- [ ] **Security — path traversal:** The filename in `log_source=file:{…}`
  must be validated against the list returned by `/logs/files` on the server
  side before being forwarded to the orchestrator. This is already called out
  in the route step above.

- [ ] **`orchestrator.log` and container log directories don't collide:**
  Container log dirs live at `{log_dir}/{container_id}/` (subdirectories).
  `orchestrator.log` lives at `{log_dir}/orchestrator.log` (a file at the
  root). No conflict.
