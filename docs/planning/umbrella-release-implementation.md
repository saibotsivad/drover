# Umbrella Release — Implementation Plan

> Draft for team review — not yet adopted. Companion to
> [`docs/releases.md`](../releases.md) (the as-is description of the release
> strategy) and the ADR at
> [`docs/decisions/2026-05-23-github-release-as-manifest.md`](../decisions/2026-05-23-github-release-as-manifest.md).
> This document covers only the *how* of building the umbrella release step.

---

## Goal / Desired Outcome

After every successful release of one or more components, the repository
automatically publishes a GitHub Release whose body is a cross-link
manifest. The release:

- Has version `v$YEAR.$MONTH-$INCREMENT` (CalVer with in-month increment).
- Contains `manifest.yaml`, `manifest.yaml.sig`, `checksums.txt`, and
  `install.sh` as assets.
- Cross-links to GHCR images by digest, to PyPI packages (when applicable),
  and to per-component releases for binaries.
- Is reachable by `releases/latest/download/<asset>` for stable install
  URLs.

A new engineer should be able to follow the checklists below in order
without needing to reverse-engineer the existing workflows.

---

## Background

Today, `push-tag.yml` reads changed `*/CHANGELOG.yml` files on the merged
`versioning` PR, pushes per-project git tags, and dispatches
`publish-image.yml` once per affected project. The umbrella release step
runs after those finish. Its inputs are:

- The per-project versions emitted by `push-tag.yml`'s `detect` job.
- The image digests emitted by each `publish-image.yml` run.
- The previous release on GitHub (used to compute the new increment and to
  carry forward unchanged components).

The work breaks into five concerns:

1. **Schema & helper script** — define `manifest.yaml`, write a Python
   script that assembles it.
2. **Workflow wiring** — add `umbrella-release.yml`, call it from
   `push-tag.yml`, plumb digests through.
3. **Signing** — cosign-sign the manifest.
4. **Installer** — write `scripts/install.sh` that consumes the manifest.
5. **Manual re-run path** — `workflow_dispatch` entrypoint for recovery.

---

## Component layout

```
.github/workflows/umbrella-release.yml     New. workflow_call + workflow_dispatch.
scripts/build_manifest.py                  New. Assembles manifest.yaml.
scripts/install.sh                         New. Consumer-facing installer.
scripts/release_assets/                    New folder; staging area used in CI.
docs/releases.md                           Already exists; reference doc.
docs/decisions/2026-05-23-github-release-as-manifest.md   Already exists; ADR.
```

`publish-image.yml` is updated to emit the pushed image digest as a job
output. `push-tag.yml` is updated to call `umbrella-release.yml` after the
per-component publishes finish.

---

## Manifest schema

The full schema is documented in [`docs/releases.md`](../releases.md). For
implementation purposes the engineer needs to know:

- Top-level keys: `drover` (string, CalVer), `released` (ISO 8601 UTC),
  `components` (object).
- Every project listed in `docs/versioning.md`'s "Versioned projects" table
  appears under `components`, regardless of whether it changed in this
  release.
- Container components carry `version`, `image` (full reference including
  tag), `digest` (`sha256:...`).
- PyPI components carry `version` and `pypi` (PEP 508 spec).
- CLI carries `version`, `release` (URL to per-component GitHub release),
  and `assets` (map of `<os>-<arch>` → `{url, sha256}`).

Unchanged components are populated by reading the previous release's
manifest and copying their entries forward.

---

## Implementation checklists

### 1. Manifest builder script (`scripts/build_manifest.py`)

- [ ] Accept inputs via env vars or argparse:
  - `--previous-manifest` (path to last release's `manifest.yaml`, may be
    absent for the very first umbrella release).
  - `--component <name>=<version>:<image>@<digest>` (repeatable, for
    container components that were just published).
  - `--released-at` (ISO 8601 UTC; defaults to now).
  - `--drover-version` (the CalVer string computed upstream).
  - `--output` (path to write the assembled manifest).
- [ ] For each project in the canonical list (read from
  `docs/versioning.md`'s table, or a hard-coded list in the script kept in
  sync — pick one and call it out in a comment):
  - [ ] If the project appears in `--component` arguments, use those values.
  - [ ] Else, copy the entry from `--previous-manifest`.
  - [ ] Else (first release ever, no previous), read `published` from the
    project's `CHANGELOG.yml` and synthesize the image reference from the
    known image-suffix mapping.
- [ ] For the CLI: read a sidecar JSON file produced by the per-CLI release
  job (`cli-release-assets.json`) describing each platform's URL and
  SHA-256. Schema documented in section 4 below.
- [ ] Write `manifest.yaml` with deterministic key ordering (use
  `yaml.safe_dump(..., sort_keys=False)` and an explicit dict order so
  diffs between releases stay readable).
- [ ] Unit tests under `tests/release/test_build_manifest.py`:
  - [ ] No previous manifest, one component changed.
  - [ ] Previous manifest, no components changed (no-op release, should
    error — see "Edge cases" below).
  - [ ] Previous manifest, one container changed, CLI unchanged.
  - [ ] Previous manifest, CLI changed, containers unchanged.
  - [ ] Schema validation against a vendored JSON schema.

### 2. CalVer version computation

- [ ] Implement as a step in `umbrella-release.yml` (small enough to be a
  bash one-liner with `gh api`; not worth a separate script).
- [ ] Query `gh api repos/<owner>/<repo>/releases?per_page=10` and filter
  for tags matching `v\d{4}\.\d{1,2}-\d+`.
- [ ] Compute the new version:
  - Parse the newest matching release.
  - If its `$YEAR.$MONTH` equals the current UTC year/month, set the new
    increment to `previous_increment + 1`.
  - Otherwise, set the new increment to `1`.
- [ ] Emit `drover_version` as a step output for downstream jobs to consume.
- [ ] Edge case: pre-releases (any release with `prerelease=true`) are
  ignored when computing the increment.
- [ ] Edge case: `make_latest=false` releases (backports) are also ignored
  when computing the next forward increment — but they still need their own
  CalVer string at publish time. For the manual-rerun path, the engineer
  passes the version explicitly; the auto path always computes forward from
  the newest forward release.

### 3. New workflow: `umbrella-release.yml`

- [ ] `on:` block supports both `workflow_call` (from `push-tag.yml`) and
  `workflow_dispatch` (for manual re-run).
- [ ] `workflow_call` inputs:
  - [ ] `orchestrator_version`, `orchestrator_digest`
  - [ ] `builder_version`, `builder_digest`
  - [ ] `webapp_version`, `webapp_digest`
  - [ ] `executor_version` (string, may be empty if unpublished)
  - [ ] `cli_version` (string, may be empty if CLI release isn't part of
        this run)
- [ ] `workflow_dispatch` inputs:
  - [ ] `drover_version` (required; explicit CalVer string).
  - [ ] `dry_run` (boolean; if true, build and upload-artifact but don't
        create a release).
- [ ] Permissions: `contents: write` (create release, push tag),
  `id-token: write` (cosign keyless OIDC), `packages: read` (read GHCR
  manifests for digests if needed).
- [ ] Jobs:
  - [ ] `compute-version`: only on `workflow_call`. Runs the CalVer step
        above. Output: `drover_version`.
  - [ ] `fetch-previous`: download the previous release's `manifest.yaml`
        (use `gh release download` against `latest`, tolerating a 404 on the
        very first release).
  - [ ] `build-manifest`: run `scripts/build_manifest.py` with the inputs
        from the calling workflow and the previous manifest.
  - [ ] `sign-manifest`: `cosign sign-blob --yes manifest.yaml > manifest.yaml.sig`
        using keyless OIDC. Same signing identity as `publish-image.yml`.
  - [ ] `build-checksums`: SHA-256 every staged asset into `checksums.txt`.
  - [ ] `create-release`: `gh release create v$DROVER_VERSION --notes-file
        notes.md manifest.yaml manifest.yaml.sig checksums.txt install.sh`.
        Use `--latest` for forward releases, `--latest=false` for the
        manual backport path.
- [ ] Concurrency group keyed on the repository, so two release PRs
  merging in quick succession don't race on the CalVer counter. Group:
  `umbrella-release-${{ github.repository }}`, `cancel-in-progress: false`.

### 4. Plumbing through `push-tag.yml` and `publish-image.yml`

- [ ] In `publish-image.yml`, add `outputs:` exposing the pushed image
  digest. `docker/build-push-action@v6` already exposes `digest` on its
  step outputs; re-emit it as a workflow output.
- [ ] In `push-tag.yml`:
  - [ ] Capture `publish-orchestrator.outputs.digest`,
        `publish-builder.outputs.digest`, `publish-webapp.outputs.digest`.
  - [ ] Add a new `umbrella` job with
        `needs: [detect, publish-orchestrator, publish-builder, publish-webapp]`
        and `if: always() && needs.detect.result == 'success' &&
        (needs.publish-orchestrator.result == 'success' ||
         needs.publish-orchestrator.result == 'skipped') && …`
        (i.e. every publish either succeeded or was skipped).
  - [ ] The `umbrella` job uses `./.github/workflows/umbrella-release.yml`
        and passes versions + digests.
- [ ] If every publish was `skipped` (no components changed), the umbrella
  job exits cleanly without creating a release. There is no umbrella
  release for a no-op release-PR merge.

### 5. CLI release coordination

The CLI lives outside the current container-only release scheme. The
umbrella manifest needs CLI asset URLs and checksums. Two integration
points:

- [ ] The CLI's `goreleaser` config (defined in the CLI implementation
  plan) writes a `cli-release-assets.json` to its job artifacts directory.
  Schema:

  ```json
  {
    "version": "1.0.2",
    "release_url": "https://github.com/.../releases/tag/cli-v1.0.2",
    "assets": {
      "linux-amd64":  { "url": "...", "sha256": "..." },
      "linux-arm64":  { "url": "...", "sha256": "..." },
      "darwin-amd64": { "url": "...", "sha256": "..." },
      "darwin-arm64": { "url": "...", "sha256": "..." },
      "windows-amd64":{ "url": "...", "sha256": "..." }
    }
  }
  ```

- [ ] When the CLI release job runs in the same release PR as the umbrella
  release, it uploads this JSON as a workflow artifact named
  `cli-release-assets`. The umbrella job's `build-manifest` step downloads
  that artifact and points the builder script at it.
- [ ] When the CLI didn't change, the umbrella job skips the artifact
  download and the builder carries the CLI block forward from the previous
  manifest.

### 6. Installer (`scripts/install.sh`)

- [ ] Detect platform: `uname -s` (Linux/Darwin/MINGW…), `uname -m` (x86_64,
  arm64, aarch64). Normalize to the manifest's `<os>-<arch>` keys.
- [ ] Download `manifest.yaml` and `manifest.yaml.sig` from the same
  release the script came from. Determine "same release" via:
  - When invoked via `releases/latest/download/install.sh`, hard-code the
    URL `https://github.com/<owner>/<repo>/releases/latest/download/manifest.yaml`.
  - Allow override via `$DROVER_RELEASE` (a tag like `v2026.5-3`) for
    pinning.
- [ ] Verify the signature with `cosign verify-blob --certificate-identity
  ... --certificate-oidc-issuer ...`. Skip with a clear warning if `cosign`
  isn't installed and `$DROVER_SKIP_SIG_VERIFY=1` is set; otherwise error.
- [ ] Parse `components.cli.assets[$os-$arch].url` and `.sha256`. Avoid
  taking a hard dependency on `yq` — the manifest is constrained enough
  that a small `awk`/`grep` parser works, but if `yq` is available prefer
  it. (Make a deliberate call here; document the choice in the script
  header.)
- [ ] Download the binary tarball, verify SHA-256, extract to a temp dir,
  move the binary to `${DROVER_INSTALL_DIR:-/usr/local/bin}/drover`.
- [ ] Print the installed version on success. Exit non-zero on any failure
  with a clear message.
- [ ] Manual test matrix before first release:
  - [ ] Linux amd64
  - [ ] Linux arm64
  - [ ] macOS arm64
  - [ ] (Optional) Windows under Git Bash

### 7. Release notes body

- [ ] `umbrella-release.yml` assembles the GitHub Release body (passed via
  `--notes-file`) by:
  - [ ] Including a "What's new" section that aggregates each component's
        new CHANGELOG entries for this release (read the per-project
        `CHANGELOG.yml` and emit the entries from the newest version block
        only).
  - [ ] Including a "Pinned versions" table (one row per component).
  - [ ] Linking to the manifest and signature.
- [ ] Implement as a small Python helper, `scripts/build_release_notes.py`,
  invoked next to `build_manifest.py`. Output to `notes.md` in the staging
  dir.

### 8. Tests and validation

- [ ] Unit tests for `build_manifest.py` (covered in section 1).
- [ ] Unit tests for the CalVer increment logic (extract to a tiny Python
  helper if the bash gets gnarly).
- [ ] Integration test: a fixture directory containing a previous
  manifest, fake CLI assets JSON, and expected output `manifest.yaml`.
  Snapshot-test the builder.
- [ ] Manual smoke test before first real release: run the workflow with
  `dry_run: true` against the current state of `main`. Inspect the
  uploaded workflow artifacts (manifest, signature, checksums, install.sh)
  by hand.

### 9. Documentation updates

- [ ] Cross-link `docs/releases.md` from the repo `README.md` Quickstart so
  users know where to find the install instructions.
- [ ] Add a short paragraph to `docs/versioning.md` pointing readers to
  `docs/releases.md` for the umbrella layer.
- [ ] Once the first umbrella release ships, delete the "Draft for team
  review" header at the top of this planning doc.

---

## Edge cases to confirm before merging

- [ ] **First umbrella release ever.** No previous manifest. The script
  builds from `CHANGELOG.yml` published values and the canonical project
  list. CalVer starts at `v$YEAR.$MONTH-1`.
- [ ] **Release PR merges with no `CHANGELOG.yml` changes.** Shouldn't
  happen given the existing flow, but if it does, the umbrella job's
  `if:` condition skips and no release is created.
- [ ] **A component publish failed.** The umbrella job's `if:` requires
  every publish to be `success` or `skipped`. A failed publish prevents
  the umbrella release.
- [ ] **Two release PRs merge within seconds.** The concurrency group on
  `umbrella-release.yml` serializes them; the second sees the first's
  release when computing its CalVer increment.
- [ ] **Backport for an older Drover version.** Triggered manually via
  `workflow_dispatch` with an explicit `drover_version` and
  `make_latest=false`. The auto-increment path is not used.
- [ ] **Cosign signing fails.** Surface the error and abort; no
  half-signed release.
- [ ] **PyPI publish doesn't happen in the same run** (executor lands on
  PyPI through a separate workflow once that exists). Treat the executor
  manifest entry as "carried forward from previous" unless the workflow
  was explicitly told a new version exists.

---

## Out of scope for the first cut

- A separate `drover` HTTP endpoint or site serving `install.sh`. The
  `releases/latest/download/` URL is sufficient for v1.
- Homebrew, apt, or Scoop package channels. They can follow once the
  release flow is stable.
- Automated changelog diffing between umbrella releases. The per-component
  CHANGELOGs are the source; cross-release diffs can be added later.
- Re-uploading large component artifacts to the umbrella release. The
  manifest cross-links and that is the deliberate design.

---

## Suggested implementation order

1. **Schema + manifest builder** (`scripts/build_manifest.py`) with unit
   tests. No CI integration yet; the script runs locally against a
   hand-crafted previous manifest.
2. **Release notes builder** (`scripts/build_release_notes.py`) likewise.
3. **`umbrella-release.yml`** with `workflow_dispatch` only, `dry_run`
   default true. Validate by hand.
4. **Plumb digests** through `publish-image.yml` and `push-tag.yml`,
   then wire `umbrella-release.yml` as a `workflow_call` from
   `push-tag.yml`.
5. **Cosign signing** added to the workflow once the unsigned path works
   end-to-end.
6. **Installer** (`scripts/install.sh`) and the manual platform test
   matrix.
7. **First real umbrella release** on a non-trivial component change.
   Verify `releases/latest/download/install.sh` resolves and installs.
8. **Documentation pass**: remove the draft header here, link from
   `README.md` and `versioning.md`.

Steps 1–3 are safe to land without affecting the existing release flow.
Step 4 is the first step that changes live workflow behaviour, so it
should land behind a feature flag (a workflow input that defaults to
"skip umbrella") that is flipped only once steps 5–6 are also ready.
