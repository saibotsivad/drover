# Test Selector Conventions

Playwright tests select elements using standard `id` and `class` attributes.
No `data-testid` or other test-only markup.

## Principles

**`id`** marks a unique anchor per page render: a specific data row, the active
log viewer, the metadata block that HTMX replaces. Use an `id` when there is at
most one of the thing on the page at any moment.

**`class`** marks typed or recurring elements: button actions, status badges, empty
states. Use a class when an element is one of several of the same kind, or when
the name identifies the *role* rather than the *instance*.

A selector typically combines both: `#worker-abc123 .btn-stop`,
`select.log-source-select`, `tbody#worker-rows tr`.

---

## Page sections

Each view's outermost `<section>` carries a stable `id` naming the view. This
gives tests a scoped root that survives HTMX partial replacements of inner
elements.

| View | `id` |
|------|------|
| `/views/workers` | `workers-list` |
| `/views/workers/:id` | `worker-detail` |
| `/views/workers/:id/execs/:commandId` | `exec-detail` |
| `/views/images` | `images-list` |
| `/views/launch` | `launch-form` |

---

## Data tables

The `<tbody>` of every data table carries an `id` of the form `{resource}-rows`.
Individual rows that represent a keyed record carry an `id` of the form
`{resource}-{key}`.

```
tbody#worker-rows             worker list body (also the HTMX refresh target)
tr#worker-{id}                one worker row; key is the worker UUID

tbody#image-rows              image list body
tr#image-{name}               one image row; key is the drover.name label value
```

The key is the raw value from the API with no transformation. Worker UUIDs
(hex + hyphens) and drover image names (lowercase slugs) are both safe as CSS
id values without encoding.

---

## Interactive elements

### Form fields

Each `<input>`, `<select>`, and `<textarea>` uses an `id` that matches its
`name` attribute. This is already the convention and no change is needed.

### Action buttons

Buttons that trigger a domain action carry a semantic class in addition to their
visual-style class. The semantic class describes what the button does, not how it
looks:

| Action | Semantic class | Visual class |
|--------|---------------|--------------|
| Stop a worker | `btn-stop` | `btn-secondary` |
| Destroy a worker | `btn-destroy` | `btn-danger` |

Both classes appear on the same element. A test selects `#worker-abc123
.btn-stop`; CSS targets `.btn-secondary`. The two concerns stay independent.

A form's primary submit button is the only `button[type="submit"]` in its form,
so no additional class is needed there.

### Named selects

A `<select>` that has a domain role beyond a plain form field carries a class
that names that role, combined with the element type in selectors:

```
select.log-source-select      the log source picker on the container detail page
```

This pattern (`{role}-select` as a class, `select.{role}-select` as a selector)
extends to any future select that needs to be targeted independently of its
position in the page.

---

## Status badges

Status pills use `class="status status-{slug}"` where `{slug}` is the lowercase
status string from the API (`running`, `stopped`, `error`, etc.). Tests assert
the active states they care about:

```
.status-running
.status-initializing
.status-stopped
.status-error
```

---

## Quick reference

| Element | Convention | Example selector |
|---------|-----------|-----------------|
| Page section | `id="{view}"` on `<section>` | `#containers-list` |
| Table body | `id="{resource}-rows"` on `<tbody>` | `#container-rows` |
| Keyed row | `id="{resource}-{key}"` on `<tr>` | `#container-abc123` |
| Form field | `id="{field_name}"` on input/select/textarea | `#privileged` |
| Action button | class `btn-{action}` alongside visual class | `#container-abc123 .btn-stop` |
| Named select | class `{role}-select`, selected as element+class | `select.log-source-select` |
| Status badge | class `status-{slug}` | `.status-running` |
