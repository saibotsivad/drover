# Exec Command Panel

This document describes the planned UI additions for viewing exec commands on the container detail page and a new dedicated exec output view.

---

## Overview

Two interface additions are needed:

1. **Exec commands list** — a new section on `/views/containers/{container_id}`, placed between the action bar and the logs section, showing all commands that have been run against the container.
2. **Exec output view** — a new route `/views/containers/{container_id}/exec/{command_id}` showing the interleaved stdout/stderr output for a single command.

Exec commands are stateless (each is an independent subprocess — no shared shell state between commands), so there is no "session" concept to surface. The list is a historical record.

---

## Required Orchestrator Change

There is currently no endpoint to list all commands for a container. The webapp needs one. This must be added before the frontend work.

### New model (`orchestrator/models.py`)

Add a `CommandSummary` model alongside the existing `ExecStatusResponse`:

```python
class CommandSummary(BaseModel):
    command_id: str
    command: str
    status: CommandStatus
    exit_code: int | None = None
    created_at: str
```

### New container manager method (`orchestrator/container_manager.py`)

Add `list_commands(container_id: str) -> list[CommandSummary]`. Query the `commands` table filtered by `container_id`, ordered by `created_at DESC` (newest first). Raise `ContainerNotFound` if the container row does not exist (consistent with how other methods on this manager behave).

### New route (`orchestrator/routers/containers.py`)

```
GET /containers/{container_id}/exec  →  list[CommandSummary]
```

Returns an empty list if the container exists but has no commands. Returns 404 if the container does not exist.

---

## Container Detail Page Changes

### New section: Exec Commands

Add an exec commands section to `containerDetailPage()` in `webapp/src/views/partials/container-detail.js`, inserted after `actionBar(container)` and before the `logsSection`.

The section renders a table of all commands run against the container. If no commands have been run, show a muted empty-state message.

#### Markup structure

```html
<section class="exec-section">
  <h3>Exec Commands</h3>
  <table class="data-table">
    <thead>
      <tr>
        <th>Command</th>
        <th>Status</th>
        <th>Exit code</th>
        <th>Started</th>
      </tr>
    </thead>
    <tbody id="command-rows">
      <!-- one tr per command -->
      <tr id="command-{command_id}">
        <td><a href="/views/containers/{container_id}/exec/{command_id}"><code>{command}</code></a></td>
        <td><span class="status status-{status}">{status}</span></td>
        <td>{exit_code ?? '—'}</td>
        <td><time datetime="{created_at}">{created_at}</time></td>
      </tr>
      <!-- empty state -->
      <tr class="empty"><td colspan="4">No exec commands yet</td></tr>
    </tbody>
  </table>
</section>
```

#### Selector conventions

| Element | Convention |
|---------|-----------|
| Table body | `id="command-rows"` on `<tbody>` |
| Keyed row | `id="command-{command_id}"` on `<tr>` |
| Status badge | `class="status status-{slug}"` — reuse existing pill; slugs are `pending`, `running`, `complete` |

The `command` text is truncated visually via CSS if long (`max-width` + `overflow: hidden` + `text-overflow: ellipsis` on the `<td>`), but the full text is accessible via the link.

#### Data fetching

In `webapp/src/routes/views.js`, the `GET /containers/:id` handler already calls the orchestrator in parallel for the container and log files. Add a third parallel call:

```js
const [container, filesResult, commands] = await Promise.all([
    orchestrator.getJson(`/containers/${encodedId}`),
    fetchLogFiles(orchestrator, encodedId),
    fetchCommands(orchestrator, encodedId),
]);
```

`fetchCommands` is a small helper that calls `GET /containers/{encodedId}/exec` and returns the array (empty array on 404, so a missing container falls through to the existing error path from the first `getJson` call).

Pass `commands` into `containerDetailPage()` as a new parameter (alongside `logOpts`).

#### Section ID fix

The current `containerDetailPage` returns `<section>` with no `id`. Per the test selector conventions, the outermost `<section>` of each view should carry `id="{view}"`. Add `id="container-detail"` to this element as part of this change.

---

## New Exec Output View

### Route

```
GET /views/containers/:id/exec/:commandId
```

Register in `webapp/src/routes/views.js` below the existing `GET /containers/:id` handler.

The handler fetches:
1. `GET /containers/{encodedId}` — to get the container (for the page title and a "back" link)
2. `GET /containers/{encodedId}/exec/{encodedCommandId}` — to get the `ExecStatusResponse`

Both calls run in parallel. Render `execOutputPage()` from a new partial.

### Page partial (`webapp/src/views/partials/exec-output.js`)

```html
<section id="exec-detail">
  <div class="page-header">
    <h2>Exec: <code>{command}</code></h2>
    <a class="btn btn-secondary" href="/views/containers/{container_id}">Back to container</a>
  </div>

  <dl id="exec-meta" class="meta-grid">
    <!-- Status row -->
    <div class="meta-row">
      <dt>Status</dt>
      <dd><span class="status status-{status}">{status}</span></dd>
    </div>
    <!-- Exit code row — only when complete -->
    <div class="meta-row">
      <dt>Exit code</dt>
      <dd>{exit_code}</dd>
    </div>
    <!-- Command ID row -->
    <div class="meta-row">
      <dt>Command ID</dt>
      <dd><code>{command_id}</code></dd>
    </div>
  </dl>

  <!-- Output -->
  <section class="exec-output-section">
    <h3>Output</h3>
    <pre id="exec-output" class="exec-output">{interleaved chunks}</pre>
    <!-- or if no output yet: -->
    <p class="muted exec-output-empty">(no output yet)</p>
  </section>
</section>
```

#### Selector conventions

| Element | Convention |
|---------|-----------|
| Page section | `id="exec-detail"` on outer `<section>` |
| Metadata grid | `id="exec-meta"` — the HTMX-replaceable block if we ever add auto-refresh |
| Output pre | `id="exec-output"` |

The new view should be added to the conventions table in `docs/test-selector-conventions.md`:

| View | `id` |
|------|------|
| `/views/containers/:id/exec/:commandId` | `exec-detail` |

### Rendering the interleaved output

The `ExecStatusResponse.messages` array is already ordered by `seq` (ascending). Render each message as an inline `<span>` inside the `<pre>`:

- stdout messages: plain `<span class="output-chunk">{escaped data}</span>`
- stderr messages: `<span class="output-chunk output-stderr">{escaped data}</span>`

The `.output-stderr` class applies the light-red background. Because the messages are inline spans inside a `<pre>`, whitespace and newlines within each data chunk are preserved exactly.

If `messages` is empty, render the empty-state paragraph instead of the `<pre>`.

---

## CSS Additions (`webapp/public/css/styles.css`)

```css
/* --- Exec commands section (container detail) --- */

.exec-section {
    margin-top: 2rem;
}

.exec-section h3 {
    margin: 0 0 0.75rem 0;
    font-size: 1rem;
    font-weight: 600;
}

/* Truncate long commands in the table */
.exec-section .data-table td:first-child {
    max-width: 32rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

/* --- Exec output view --- */

.exec-output-section {
    margin-top: 1.5rem;
}

.exec-output-section h3 {
    margin: 0 0 0.75rem 0;
    font-size: 1rem;
    font-weight: 600;
}

.exec-output {
    margin: 0;
    padding: 0.85rem 1rem;
    border: 1px solid var(--color-border);
    border-radius: var(--radius);
    background: var(--color-bg-subtle);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.88rem;
    overflow-x: auto;
    white-space: pre;
}

.exec-output-empty {
    margin: 0;
}

.output-chunk {
    /* display inline preserves the pre's whitespace handling */
}

.output-stderr {
    background: #fde8e8;
}

@media (prefers-color-scheme: dark) {
    .output-stderr {
        background: #3d1515;
    }
}
```

---

## Files to Modify / Create

### Orchestrator (Python)

| File | Change |
|------|--------|
| `orchestrator/models.py` | Add `CommandSummary` model |
| `orchestrator/container_manager.py` | Add `list_commands()` method |
| `orchestrator/routers/containers.py` | Add `GET /{container_id}/exec` route |

### Webapp (Node/JS)

| File | Change |
|------|--------|
| `webapp/src/routes/views.js` | Add `GET /containers/:id/exec/:commandId` route; extend `GET /containers/:id` to also fetch commands |
| `webapp/src/views/partials/container-detail.js` | Add exec commands section; add `id="container-detail"` to outer section; accept `commands` param |
| `webapp/src/views/partials/exec-output.js` | **New file** — exec output page partial |
| `webapp/public/css/styles.css` | Add `.exec-section`, `.exec-output`, `.output-stderr`, and related rules |

### Docs

| File | Change |
|------|--------|
| `docs/test-selector-conventions.md` | Add `exec-detail` to the page sections table |

---

## Out of Scope

- Auto-refresh of the exec list or output view (polling). The WebSocket streaming plan covers real-time updates; this UI targets the polling-only baseline.
- A form for submitting new exec commands from the web UI. That would be a separate feature.
- Pagination of the command list or output messages. Not needed at this scale.
