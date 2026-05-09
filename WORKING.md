# Working Checklist — Changeset Automation

Tracking sheet for the changeset-automation work. The original planning
doc (`docs/planning/changeset-automation.md`) was retired once
implementation landed; the permanent reference is now `docs/versioning.md`.
Tick items as they land. If work is interrupted, this file is the source of truth
for "what's left" — prefer updating it over relying on commit history.

Conventions:
- `[ ]` = not started, `[~]` = in progress, `[x]` = done.
- Each phase should land as one or more focused commits.
- Don't skip phases; later phases assume earlier scaffolding exists.

---

## Phase 0 — Decisions to lock in

- [x] Confirm the set of versioned projects. Plan examples list `orchestrator`,
      `builder`, `webapp`. Repo also contains `executor/` — decide whether it
      participates (own CHANGELOG + tag prefix) or is rolled into another
      project. Record the decision here:
      - Decision: `builder`, `executor`, `orchestrator`, `webapp` all get
        their own `CHANGELOG.yml` and tag prefix. `executor` is versioned but
        is **not** published as a Docker image, so it has no publish workflow
        in Phase 5 — its tags exist purely as a release record.
- [x] Confirm the release branch name. Plan says `release/next`; flag if a
      different convention is preferred before workflows are written.
      - Decision: `versioning` (replaces `release/next` everywhere in this
        checklist and in the implementation).
- [x] Confirm tag format. Plan says `<project>-v<version>` (e.g.
      `orchestrator-v0.2.0`). Lock this before touching publish workflows.
      - Decision: confirmed — `<project>-v<version>`.

---

## Phase 1 — Repo scaffolding

- [x] Create `changes/` directory at repo root with a `.gitkeep` so it exists
      when empty.
- [x] Add `changes/README.md` explaining the change-file format with a minimal
      example. Link to the versioning doc for details (originally pointed at
      the planning doc; later updated to `docs/versioning.md` after the
      planning doc was retired).
- [x] Create initial `CHANGELOG.yml` in each versioned project directory,
      seeded with `published: "0.0.0"` and an empty `changes: []` list. Files
      to create: `builder/CHANGELOG.yml`, `executor/CHANGELOG.yml`,
      `orchestrator/CHANGELOG.yml`, `webapp/CHANGELOG.yml`.
- [x] Add `PyYAML` to a new `requirements-release.txt` (kept separate from
      `requirements-test.txt` to avoid bloat).

---

## Phase 2 — `scripts/update_release_pr.py`

The single script invoked by Workflow 1. Build it incrementally; each bullet
should be testable in isolation before moving on.

- [x] Module skeleton: argparse entry point, `main()`, and a `--dry-run` flag
      that prints planned actions without writing.
- [x] Loader: read every `*.yaml` / `*.yml` under `changes/`, parse, and
      validate shape (top-level list; each entry has `project`, `bump`,
      `description`; `bump` ∈ {major, minor, patch}). Fail loudly with the
      offending file path on invalid input.
- [x] Grouping: bucket entries by project. Reject entries whose `project` does
      not correspond to an existing top-level directory containing a
      `CHANGELOG.yml`.
- [x] Version math: implement highest-bump-wins per project. Read current
      `published` from each affected `CHANGELOG.yml`; compute the next version.
      Helpers: `parse_semver`, `bump_semver(current, level)`.
- [x] Changelog writer: prepend a new entry to each affected `CHANGELOG.yml`
      with today's date (UTC) and update `published`. Preserve YAML block
      style and the `|` literal scalars on `description`.
- [x] PR-body builder: render the Markdown summary grouped by project in the
      exact format from the plan (heading per project with old → new and bump
      level; bullets per entry).
- [x] Cleanup: delete consumed files from `changes/`.
- [x] Local smoke test: hand-craft a couple of change files in a scratch dir,
      run `--dry-run`, verify the planned bumps, PR body, and deletions.

---

## Phase 3 — Workflow 1: `update-release-pr.yml`

- [x] Create `.github/workflows/update-release-pr.yml`.
- [x] Trigger on `push` to `main`; add `paths: ['changes/**']` guard so it
      no-ops on unrelated pushes.
- [x] Permissions: `contents: write`, `pull-requests: write`.
- [x] Steps:
  - [x] Checkout `main` (default shallow checkout — full history isn't needed
        while the project is still in flux).
  - [x] Set up Python and install release requirements (PyYAML).
  - [x] Configure `git` user (e.g. `github-actions[bot]`).
  - [x] Run `scripts/update_release_pr.py`. If it reports "no change files"
        (exit 2), exit the job successfully without further steps.
  - [x] Create branch `versioning` from current `main` state, commit the
        edits as a single commit, force-push.
  - [x] Use `gh pr view versioning` to detect existing PR; if missing,
        `gh pr create`; if present, `gh pr edit --body`. Title is static
        ("Release: pending changes").
- [x] Verify the workflow file (visual review + YAML parse).

---

## Phase 4 — Workflow 2: `publish-release.yml`

- [x] Create `.github/workflows/publish-release.yml`.
- [x] Trigger: `pull_request` `closed`, gated by
      `head.ref == 'versioning' && merged == true`.
- [x] Permissions: `contents: write` (to push tags).
- [x] Steps:
  - [x] Checkout with `fetch-depth: 2` so `HEAD~1..HEAD` works.
  - [x] Compute changed `*/CHANGELOG.yml` paths via `git diff --name-only
        HEAD~1 HEAD`.
  - [x] For each, parse the `published` field (Python one-liner with
        PyYAML — same dependency as Workflow 1).
  - [x] Create `<project>-v<version>` annotated tag and push it.
- [x] Parse one-liner verified against a real CHANGELOG.yml. Full
      diff-against-prior-commit dry run deferred to Phase 7 (sandbox
      blocked unsigned commits in a scratch repo).

---

## Phase 5 — Update existing publish workflows

Both files currently trigger on `tags: "v*"`. They must be updated to listen
for project-prefixed tags and to extract the bare semver via
`docker/metadata-action`'s `type=match`.

Note: `executor` is versioned but not published, so no publish workflow is
created for it. `executor-v*` tags will be pushed by Workflow 2 and simply
have no listener — that's intentional.

- [x] `.github/workflows/publish.yml`
  - Approach: keep a **single file** for both container projects
    (`orchestrator`, `builder`); split the existing matrix into two jobs, each
    gated on its tag prefix via `if: startsWith(github.ref_name, '<prefix>-v')`.
  - [x] Update `tags:` trigger list to include both `orchestrator-v*` and
        `builder-v*`.
  - [x] Replace the matrix with two jobs (`publish-orchestrator`,
        `publish-builder`); each runs only when its prefixed tag is pushed.
  - [x] In each job's `docker/metadata-action`, replace the `type=semver`
        lines with three `type=match` patterns covering full / major.minor /
        major. Using regex anchors (`\d+\.\d+\.\d+` etc.) keeps the captured
        Docker tag unprefixed.
- [x] `.github/workflows/publish-webapp.yml`
  - [x] Update `tags:` filter to `webapp-v*`.
  - [x] Same `type=match` change for metadata extraction.
- [x] Confirm published Docker tags remain unprefixed (`1.2.0`, `1.2`, `1`).
      The `type=match` patterns capture only the bare semver via group=1, so
      Docker tags do not carry the project prefix — only the git tags do.

---

## Phase 6 — Contributor docs

- [x] Add a "Releasing" section to `README.md` covering: how to write a change
      file, where it goes, what happens when the PR merges, and what the
      release PR looks like.
- [x] Cross-link from the planning doc once implementation lands (later
      superseded: the planning doc was retired in favour of
      `docs/versioning.md`, which is now the permanent reference).
- [x] Update `TODO.md` / `PLAN.md` if either references release tooling.
      `TODO.md` has a "Verify GHCR publish workflow (manual)" note — left
      as-is, since the manual end-to-end verification is still pending and
      now naturally subsumes the new tag scheme as part of Phase 7.

---

## Phase 7 — End-to-end verification

- [ ] On a throwaway branch, drop a sample change file touching one project,
      open a PR, merge it, and confirm:
  - [ ] `versioning` branch is created with the expected diff.
  - [ ] Release PR opens with the right body.
- [ ] Add a second change file in a follow-up PR for a different project,
      merge it, and confirm the existing release PR is updated (single
      commit, body extended).
- [ ] Merge the release PR and confirm:
  - [ ] Project-prefixed tags are pushed.
  - [ ] Each prefixed tag triggers exactly one publish workflow and produces
        unprefixed Docker tags.
- [ ] Once green, delete the sample CHANGELOG entries / revert version bumps
      on the test project (or keep them as the genuine first release —
      decide before running the test).
- [ ] While verifying, also confirm the SHA-tag flow added during review:
      a merge to `main` touching one of the publishable project paths
      should produce a `sha-<short>` Docker tag in GHCR with no version
      tags attached, and `executor`-only changes should produce no image
      at all.
- [ ] Run `prune-ghcr.yml` once via `workflow_dispatch` with `dry-run: true`
      to confirm the selection only matches SHA-only versions and never
      release versions. Do this before the first scheduled run fires.
- [ ] On a throwaway PR, confirm `pr-changeset-summary.yml` posts a sticky
      comment in all three states: no change file (reminder), valid change
      file (rendered summary), and intentionally-malformed change file
      (validation error surfaced in the comment). Push another commit to
      the PR and confirm the existing comment is *edited*, not duplicated.

---

## Follow-ups landed during team review

- SHA-only Docker builds on every merge to `main` (publish workflows now
  trigger on `branches: [main]` in addition to release tags; a new
  `detect-changes` job filters per-project paths). Documented in
  `docs/versioning.md` under "Pre-release SHA images".
- `prune-ghcr.yml`: weekly scheduled workflow that deletes SHA-only GHCR
  versions older than 30 days. Uses `gh api` + `jq` rather than
  `actions/delete-package-versions` because the official action's
  filters can't see container tags — it can only match on the digest
  name. Release-tagged versions are protected by structure: the filter
  requires *all* tags on a version to start with `sha-`, which release
  versions never satisfy.
- `pr-changeset-summary.yml`: posts (and re-edits) a sticky comment on
  every non-`versioning` PR summarising the change files it adds,
  reminding the contributor about versioning if there are none, or
  surfacing validation errors if the change file is malformed. The
  release script gained a `--files` flag for this purpose so the same
  rendering code drives both the comment and the actual release PR.

---

## Out of scope (explicit non-goals)

These are listed in the plan and intentionally not on the checklist:

- CI check that every PR includes a change file.
- npm publishing.
- Markdown CHANGELOG generation.
