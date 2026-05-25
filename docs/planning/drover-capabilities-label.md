# Plan: `drover.capabilities` Image Label

## Summary

Add a `drover.capabilities` label to Drover-managed images that advertises, as
a comma-separated list, the set of capabilities a container launched from that
image actually supports. The webapp reads this label at render time and
conditionally disables UI controls that require a capability the image does not
declare.

Initially only the **builder** image needs this label, but the mechanism is
designed to scale as new capabilities are defined.

---

## Label Name

The existing Drover label convention is the `drover.*` namespace:

| Label | Meaning |
|---|---|
| `drover.managed` | Marks the image for discovery by the orchestrator |
| `drover.name` | Short name used to refer to the image (e.g. `builder`) |

The new label follows that convention:

```
drover.capabilities="exec,host-docker"
```

> **Note on the user's original suggestion (`docker.capabilities`):** the
> `docker.*` prefix is used by Docker's own tooling (e.g. `docker.description`,
> `docker.url`). Using it for a project-specific label risks future collisions.
> More concretely, the orchestrator's `_drover_labels()` helper already strips
> all non-`drover.*` labels before exposing them via the API, so a
> `docker.capabilities` label would be invisible to the webapp without an
> additional code change. `drover.capabilities` avoids both problems.

---

## Capability Definitions

This is the authoritative list of capability keys. A new key should be added
here before it appears on any image label.

| Key | What it grants | Notes |
|---|---|---|
| `exec` | The container responds to exec requests via the orchestrator socket. Commands submitted through the "Exec Commands" UI will be queued and executed by the guest agent (`drover-executor`). | All images that ship `drover-executor` as their `CMD` should declare this. |
| `host-docker` | The container can be launched in privileged mode: the host Docker socket is bind-mounted at `/run/docker.sock` and gVisor isolation is disabled. This allows Docker CLI commands (build, pull, push, tag) to reach the host daemon. | Only images that explicitly need Docker-in-Docker style access should declare this. Requires `PRIVILEGED_IMAGE` to be configured on the orchestrator. |

> **Why `host-docker` instead of `privileged`?**
>
> "Privileged" is overloaded: Docker has its own `--privileged` flag (broad
> Linux capability grant), and Linux itself has privileged processes. In
> Drover, what is actually granted is access to the *host Docker daemon* via a
> bind-mounted socket — gVisor is lifted as a side-effect, not as the goal.
> Naming the capability `host-docker` is more self-documenting and makes the
> security implication explicit to anyone reading the label.
>
> The webapp's "Privileged" checkbox and the `privileged` field in the
> orchestrator API are not renamed by this plan — that is existing API surface.
> The capability key name is only for the label metadata.

---

## Changes Required

### 1. Builder Dockerfile

Add the `drover.capabilities` label alongside the existing ones:

```dockerfile
LABEL drover.managed="true"
LABEL drover.name="builder"
LABEL drover.capabilities="exec,host-docker"
```

**File:** `builder/Dockerfile` (after line 28)

---

### 2. Orchestrator — No Changes Required

The orchestrator already:

- Filters image labels to the `drover.*` namespace in `_drover_labels()`
  (`orchestrator/models.py`).
- Returns those labels in `ImageSummary.labels` (`GET /images`).

`drover.capabilities` will be included automatically with no orchestrator
changes.

---

### 3. Webapp — Launch Form: Disable Privileged Checkbox

**File:** `webapp/src/views/partials/launch-form.js`

**Goal:** When an image is selected whose `drover.capabilities` label does not
include `host-docker`, the "Privileged" checkbox should be disabled (and
unchecked).

**Approach — data attributes + inline JS:**

The webapp's launch-form route already receives the list of available images
(objects with at least `name`). We extend the route to also pass each image's
parsed capabilities. The `<option>` elements are rendered with a
`data-capabilities` attribute containing the comma-separated capability list
(empty string if the label is absent, meaning unknown/unconstrained).

A small `<script>` block on the page reacts to the `<select>` change event:
if the chosen image has a non-empty `data-capabilities` that does not include
`host-docker`, the checkbox is disabled and unchecked.

If the image list is absent and a plain `<input type="text">` is rendered
instead (the "no images found" fallback), the checkbox remains fully enabled
— the operator typed in an arbitrary image name and we have no metadata.

**Route change (`webapp/src/routes/views.js` or wherever the launch page is
rendered):**

When building the `images` array, parse the `drover.capabilities` label:

```js
// Existing:
const images = rawImages.map(img => ({ name: img.name }))

// Updated:
const images = rawImages.map(img => ({
  name: img.name,
  capabilities: (img.labels?.['drover.capabilities'] ?? '').split(',').map(s => s.trim()).filter(Boolean),
}))
```

**Template change (`launch-form.js`):**

```html
<option
  value="${img.name}"
  data-capabilities="${img.capabilities.join(',')}"
  ${img.name === v.image ? 'selected' : ''}
>${img.name}</option>
```

Privileged checkbox updated to carry an `id` (it already has one) and be
initially disabled/enabled based on the pre-selected image:

```html
<label class="checkbox">
  <input
    type="checkbox"
    id="privileged"
    name="privileged"
    value="true"
    ${v.privileged ? 'checked' : ''}
    ${privilegedDisabled ? 'disabled' : ''}
  />
  Privileged
  ${privilegedDisabled ? html`<span class="muted">(not supported by this image)</span>` : null}
</label>
```

Where `privilegedDisabled` is computed server-side for the initially-selected
image, and updated client-side on selection change:

```html
<script>
  (function () {
    const select = document.getElementById('image');
    const checkbox = document.getElementById('privileged');
    if (!select || !checkbox) return;

    function update() {
      const opt = select.options[select.selectedIndex];
      if (!opt) return;
      const caps = (opt.dataset.capabilities || '').split(',').map(s => s.trim()).filter(Boolean);
      // No capabilities declared → image is unconstrained (e.g. manually typed name)
      if (caps.length === 0) {
        checkbox.disabled = false;
        return;
      }
      const allowed = caps.includes('host-docker');
      checkbox.disabled = !allowed;
      if (!allowed) checkbox.checked = false;
    }

    select.addEventListener('change', update);
    update(); // apply on initial load too
  })();
</script>
```

---

### 4. Webapp — Container Detail Page: Conditionally Show Exec UI

**Files:**
- `webapp/src/routes/views.js` (or wherever the container detail route lives)
- `webapp/src/views/partials/container-detail.js`

**Goal:** The "Exec Commands" input form and table are hidden when the
container's image does not declare the `exec` capability.

**How the webapp knows the image capabilities at render time:**

When rendering the container detail page the webapp already has `container.image`
(the drover short name, e.g. `"builder"`). It can call `GET /images` (which it
may already be doing for other purposes) and look up the matching image by name.

```js
// In the container detail route handler:
const images = await orchestrator.getJson('/images');
const imageInfo = images.find(img => img.name === container.image);
const capabilities = (imageInfo?.labels?.['drover.capabilities'] ?? '')
  .split(',').map(s => s.trim()).filter(Boolean);

const canExec = capabilities.length === 0 || capabilities.includes('exec');
// ^ if capabilities is empty (label absent), assume exec is supported
//   for backwards compatibility with images that predate this label.
```

Pass `canExec` into the view:

```js
containerDetailPage(container, logOpts, canExec ? commands : null, { canExec })
```

**Template change (`container-detail.js`):**

```js
// Current (line 181-182):
${commands ? execInputSection(container.id) : null}
${commands ? execSection(container.id, commands) : null}

// Updated:
${canExec && commands !== null ? execInputSection(container.id) : null}
${canExec && commands !== null ? execSection(container.id, commands) : null}
${!canExec ? html`<section class="exec-section">
  <h3>Exec Commands</h3>
  <p class="muted">This image does not support exec commands.</p>
</section>` : null}
```

> **Fallback behaviour:** If the image is no longer present in `GET /images`
> (deleted, renamed, or the call fails) `capabilities` will be empty and
> `canExec` will default to `true`. This is the safer fallback — we degrade
> gracefully rather than hiding exec from a container that might actually
> support it. A future improvement could store capabilities at launch time in
> the containers table to remove the dependency on image availability.

---

## Implementation Sequence

The changes are independent enough to be done in any order, but this sequence
minimises risk:

1. **Add the capability table to the docs** (this file, or a dedicated
   reference doc) — done; acts as the contract.
2. **Update `builder/Dockerfile`** — a label addition, zero behaviour change,
   safe to ship immediately.
3. **Update the orchestrator images route** — verify `drover.capabilities` is
   included in the `labels` field of `ImageSummary` responses (should already
   work; add an integration test).
4. **Update the webapp launch form** — disable the privileged checkbox when
   the selected image lacks `host-docker`.
5. **Update the webapp container detail page** — hide exec UI when the image
   lacks `exec`.
6. Write/extend tests:
   - Unit test: capability parsing helper (comma splitting, trim, dedup).
   - Integration/e2e: launch form with an image that has no `host-docker` →
     privileged checkbox is disabled.
   - Integration/e2e: container detail with a no-`exec` image → exec section
     is absent.

---

## Open Questions

| Question | Default / recommendation |
|---|---|
| Should a `drover.capabilities` label with an *empty* value be treated the same as the label being absent (unconstrained)? | Yes — empty string and absent both mean "no constraint"; all UI controls remain enabled. |
| Should the webapp cache the images list to avoid an extra `GET /images` call on every container detail page load? | Out of scope for this plan; the list is small and the call is cheap. |
| Should capabilities be stored in the `containers` DB row at launch time to decouple the detail page from image availability? | Noted as a future improvement; not required for initial implementation. |
| Should `host-docker` also require that the operator has configured `PRIVILEGED_IMAGE`? | The orchestrator already enforces this at launch time (`PrivilegedNotConfigured` error). The webapp checkbox being enabled just means the image *supports* it; the request will fail at the API level if the orchestrator is not configured for it. No change needed. |
