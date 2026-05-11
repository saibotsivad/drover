# Label-Based Image Discovery

Replace the `drover/*` image name-prefix convention with Docker label-based
discovery. Images are identified by the labels `drover.managed=true` and
`drover.name=<name>` rather than by a required tag prefix.

## Motivation

The name-prefix approach requires every workload image to be locally tagged
`drover/<name>`, which makes it impossible to reference images by their
published GHCR name (e.g. `ghcr.io/saibotsivad/drover-builder:latest`).
Docker image labels are baked into the image at build time and survive
re-tagging or pulling from any registry, so they are a more portable and
extensible identity mechanism.

## New Label Contract

Every image managed by Drover must carry both labels:

| Label | Value | Required |
|---|---|---|
| `drover.managed` | `"true"` | yes |
| `drover.name` | short name used to launch containers (e.g. `"builder"`) | yes |

The `drover.*` namespace is intentionally reserved for future Drover-specific
labels (e.g. `drover.template`, `drover.version`).

Images without both labels are invisible to Drover.

## No Backwards Compatibility

All changes conform to the new label scheme. Nothing is kept for the old
name-prefix convention.

---

## Checklist

### Orchestrator — core logic

- [x] **`orchestrator/docker_client.py:72`** — `list_images()`: remove `prefix`
  parameter; change Docker API filter from `{"reference": ["drover/*"]}` to
  `{"label": ["drover.managed=true"]}`. Add support for an optional additional
  label filter (e.g. `drover.name=<name>`) so callers can look up a single
  image by name without fetching everything.

  _Note: implemented with a single `name: str | None = None` keyword argument
  that appends `drover.name=<name>` to the label filter when provided._

- [x] **`orchestrator/models.py:146–161`** — `ImageSummary.from_docker()`:
  remove the `drover/` prefix-stripping logic (`repo.removeprefix("drover/")`).
  Read the image name from `data["Labels"]["drover.name"]` instead. Update the
  comment on line 149.

- [x] **`orchestrator/models.py:169–`** — `ImageDetail.from_docker_inspect()`:
  the `short_name` parameter is currently passed in by the caller. Switch to
  reading the name from `data["Config"]["Labels"]["drover.name"]` so the caller
  doesn't need to know it in advance.

  _Note: the `short_name` parameter was removed from the signature entirely;
  callers in `orchestrator/routers/images.py` were updated accordingly._

- [x] **`orchestrator/container_manager.py:74–76`** — `ImageNotFound.__init__`:
  change error message from `f"Image 'drover/{image}' not found"` to
  `f"Image '{image}' not found"`.

- [x] **`orchestrator/container_manager.py:285–295`** — container creation
  image validation: remove the `image = f"drover/{req.image}"` line. Instead,
  look up the image by `drover.name={req.image}` label (using the updated
  `list_images`), raise `ImageNotFound` if no result, and pass the actual image
  reference (ID or full tag) to the container-create call.

  _Note: the resolved image **ID** is passed to `create_container` (rather than
  a tag) so the call is unambiguous even when the same image carries multiple
  tags. Also dropped the now-unused `ImageNotFoundError` import._

- [x] **`orchestrator/routers/images.py:16–23`** — `get_image(name)`: remove
  `docker.inspect_image(f"drover/{name}")`. Instead, find the image by
  `drover.name={name}` label, then inspect by its ID. Return 404 if the label
  lookup returns no results.

  _Note: the inspect call is wrapped in a `try/except ImageNotFoundError` so a
  race (image removed between list and inspect) still surfaces as a clean 404
  rather than a 500._

### Orchestrator — tests

- [x] **`tests/test_models.py:176`** — update `ImageSummary.from_docker()` test
  fixture: replace `"RepoTags": ["drover/python-runner:latest", ...]` with
  label-based fixture data (`"Labels": {"drover.managed": "true",
  "drover.name": "python-runner"}`). Verify `summary.name` still equals
  `"python-runner"` (line 181) via the label, not the tag.

- [x] **`tests/test_models.py:195`** — same fixture update for the second
  `ImageSummary` test case.

  _Additional tests added: a `test_from_docker_missing_labels` case for
  `ImageSummary` verifying images without `drover.name` yield an empty name,
  and a `test_from_docker_inspect_no_config` case for `ImageDetail` covering
  the defensive `Config` lookup._

- [x] **`tests/test_container_manager.py:94`** — update assertion: was
  `docker.inspect_image.assert_called_once_with("drover/python-runner")`;
  change to assert the new label-lookup behaviour instead.

  _Note: the `docker` mock fixture now stubs `list_images` to return a fake
  image with `Id: "sha256:image_abc123"`, and the test asserts the resolved
  ID is what gets passed to `create_container`._

- [x] **`tests/test_container_manager.py:140`** — update comment about
  privileged images not using the `drover/` image. Also added an assertion
  that the privileged image name is the one passed to `create_container`.

### Webapp — UI

- [x] **`webapp/src/views/partials/images-list.js:37`** — update the empty-state
  message from `No <code>drover/*</code> images.` to something that reflects
  the label requirement (e.g. `No Drover-managed images.` or similar).

  _Note: the empty-state copy now names both required labels so a user who
  built an image without them has a concrete next step._

### Webapp — tests

Review the following test fixtures that use `drover/`-prefixed image strings and
update them to match the new scheme (bare name from `drover.name` label):

- [x] **`webapp/test/proxy.test.js:159`** — `image: 'drover/example'`
- [x] **`webapp/test/orchestrator.test.js:86`** — `{ image: 'drover/x' }`
- [x] **`webapp/test/views.test.js:38`** — `image: 'drover/python-runner'`
- [x] **`webapp/test/views.test.js:50`** — `image: 'drover/node-runner'`
- [x] **`webapp/test/actions.test.js:134`** — `image: 'drover/python-runner'`
- [x] **`webapp/test/views.test.js:136`** — additional regex assertion
  `/drover\/python-runner/` updated to `/python-runner/` so the test still
  asserts the rendered image name without re-introducing the prefix.

### Builder image

- [x] **`builder/Dockerfile`** — add required labels:
  ```dockerfile
  LABEL drover.managed="true"
  LABEL drover.name="builder"
  ```

### Documentation

- [x] **`README.md:34`** — update prerequisite from "tagged with the `drover/`
  prefix" to describe the required labels.

- [x] **`README.md:148`** — update privileged container note (currently says
  "does not use a `drover/`-prefixed image").

- [x] **`README.md:198–206`** — rewrite the Image Management / Naming Convention
  section to describe the label contract instead of the `drover/` prefix.
  Remove the `docker image ls --filter=reference=drover/*` example; replace
  with `docker image ls --filter label=drover.managed=true`.

  _Note: the "Naming Convention" heading was renamed to "Label Contract" to
  reflect that the scheme is no longer about names. A short `Dockerfile`
  snippet showing the two `LABEL` directives was added under "Image Build"._

- [x] **`README.md:212`** — update "List all `drover/*` images" to reflect
  label-based listing.

- [x] **`README.md:241`** — update "validates that `drover/<image>` exists" to
  describe label-based lookup.

- [x] **`orchestrator/README.md:112`** — update `image` field description;
  remove "Resolved as `drover/<image>`" note; explain that the value must match
  a `drover.name` label on an installed image.

- [x] **`orchestrator/README.md:141`** — update endpoint description from "List
  all drover/* images" to describe label-based filtering.

- [x] **`orchestrator/README.md:145`** — rewrite the images naming section to
  describe the label contract. Includes a note that the same image can now be
  pulled from any registry (e.g. GHCR) and still be discovered by label.

- [x] **`docs/decisions/2026-04-16-drover-templates.md`** — update all
  references to the `drover/` and `drover/template-*` naming scheme. Templates
  can use an additional label (e.g. `drover.template=true`) rather than a
  distinct tag prefix. Lines 8, 46, 77, 126, 134, 181 are affected.

## Implementation Notes

- The Docker API supports compound label filters: passing multiple
  `label=key=value` entries ANDs them. `list_images(name="builder")` therefore
  issues a single request filtered by `drover.managed=true` **and**
  `drover.name=builder`, instead of fetching every Drover image and filtering
  client-side.
- Image lookups in the container-create path now resolve to an image **ID**
  rather than a tag. This means that an image tag being reassigned to a
  different image between the lookup and the create call cannot race the
  validation; Docker will create the container from the originally resolved
  image.
- No backwards-compatibility shims were added: callers passing a literal
  `drover/<name>` image string will now get a 404 from `ImageNotFound`, which
  matches the "no backwards compatibility" stance in the proposal.
