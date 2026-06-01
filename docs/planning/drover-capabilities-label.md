# Plan: `drover.capabilities` Image Label

## Summary

Add a `drover.capabilities` label to Drover-managed images that advertises, as
a comma-separated list, the set of capabilities a worker launched from that
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
drover.capabilities="exec"
```

---

## Capability Definitions

This is the authoritative list of capability keys. A new key should be added
here before it appears on any image label.

| Key | What it grants | Notes |
|---|---|---|
| `exec` | The worker responds to exec requests via the orchestrator socket. Commands submitted through the "Exec Commands" UI will be queued and executed by the worker agent (`drover-executor`). | All images that ship `drover-executor` as their `CMD` should declare this. |

---

## Changes Required

### 1. Builder Dockerfile

Add the `drover.capabilities` label alongside the existing ones:

```dockerfile
LABEL drover.managed="true"
LABEL drover.name="builder"
LABEL drover.capabilities="exec"
```

**File:** `builder/Dockerfile` (after line 28)

---

### 2. Orchestrator — Capability Enforcement

The orchestrator is the authoritative security gate. The webapp UI disables
controls as a convenience, but the orchestrator independently rejects any
request that requires a capability the image has not explicitly declared.
An absent or empty `drover.capabilities` label means *no capabilities* —
there is no implicit allowlist.

A new error class is added (alongside the existing `WorkerError` subclasses):

```python
class CapabilityNotSupported(WorkerError):
    status_code = 422
    detail = "image does not declare the required capability"
```

**File:** `orchestrator/worker_manager.py` (exec path)

Before queuing a command, the orchestrator resolves the container's `image`
field to an image, parses `drover.capabilities`, and asserts `exec` is present.
Same error if the capability is absent or the image can no longer be found
(image deleted since the container was launched: deny rather than assume).

---

### 3. Webapp — Container Detail Page: Conditionally Show Exec UI

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

const canExec = capabilities.includes('exec');
// absent or empty label = no capabilities; exec UI is hidden
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
> (deleted, renamed, or the call fails), `capabilities` will be empty and
> `canExec` will be `false` — the exec UI is hidden. The orchestrator applies
> the same deny-if-unknown rule, so the webapp and API are consistent. A future
> improvement could store capabilities in the `containers` DB row at launch time
> to remove the dependency on image availability entirely.

---

### 4. Documentation — `docs/capabilities.md`

A new file serves as the permanent, authoritative reference for capabilities.
It is the single place an image author looks to understand what labels to set,
and the single place a contributor looks before adding a new capability key.

Suggested structure:

```
# Drover Capabilities

## Overview
Brief explanation of what the drover.capabilities label is, the label format
(comma-separated keys on drover-managed images), and where enforcement happens
(orchestrator rejects requests; webapp hides unsupported controls).

## Absent or empty label
Explicitly state: an absent label or an empty string means the image declares
no capabilities. No capability-gated feature will be allowed for that image.

## Capability reference
The authoritative table of supported keys (same content as in this plan,
kept up to date here going forward):

| Key  | What it grants | Which images should declare it |
|------|----------------|-------------------------------|
| exec | ...            | ...                           |

## Adding a new capability
Short checklist: add a row to the table here, add enforcement in the
orchestrator, update the webapp, update affected Dockerfiles.
```

---

## Implementation Sequence

The changes are independent enough to be done in any order, but this sequence
minimises risk:

1. **Write `docs/capabilities.md`** — establishes the contract before any code
   changes land; reviewers can check implementation against it.
2. **Update `builder/Dockerfile`** — a label addition, zero behaviour change,
   safe to ship immediately.
3. **Add orchestrator enforcement** — capability check in the exec path, new
   `CapabilityNotSupported` error. Verify `drover.capabilities` is already
   included in `ImageSummary.labels` (it should be; add a test).
4. **Update the webapp container detail page** — hide exec UI when the image
   lacks `exec`.
5. Write/extend tests:
   - Unit test: capability parsing helper (comma splitting, trim, dedup).
   - Orchestrator integration test: exec against a container whose image lacks
     `exec` → 422.
   - Integration/e2e: container detail with a no-`exec` image → exec section
     is absent.

---

## Open Questions

| Question | Default / recommendation |
|---|---|
| Should a `drover.capabilities` label with an *empty* value be treated the same as the label being absent? | Yes — empty string and absent are both treated as *no capabilities declared*; all capability-gated features are denied. |
| Should the webapp cache the images list to avoid an extra `GET /images` call on every container detail page load? | Out of scope for this plan; the list is small and the call is cheap. |
| Should capabilities be stored in the `containers` DB row at launch time to decouple the detail page from image availability? | Noted as a future improvement; not required for initial implementation. |
