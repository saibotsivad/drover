# Working Checklist — Changeset Automation

Tracking sheet for implementing the plan in `docs/planning/changeset-automation.md`.
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
      example. Link to `docs/planning/changeset-automation.md` for details.
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

- [ ] Module skeleton: argparse entry point, `main()`, and a `--dry-run` flag
      that prints planned actions without writing.
- [ ] Loader: read every `*.yaml` / `*.yml` under `changes/`, parse, and
      validate shape (top-level list; each entry has `project`, `bump`,
      `description`; `bump` ∈ {major, minor, patch}). Fail loudly with the
      offending file path on invalid input.
- [ ] Grouping: bucket entries by project. Reject entries whose `project` does
      not correspond to an existing top-level directory containing a
      `CHANGELOG.yml`.
- [ ] Version math: implement highest-bump-wins per project. Read current
      `published` from each affected `CHANGELOG.yml`; compute the next version.
      Helpers: `parse_semver`, `bump_semver(current, level)`.
- [ ] Changelog writer: prepend a new entry to each affected `CHANGELOG.yml`
      with today's date (UTC) and update `published`. Preserve YAML block
      style and the `|` literal scalars on `description`.
- [ ] PR-body builder: render the Markdown summary grouped by project in the
      exact format from the plan (heading per project with old → new and bump
      level; bullets per entry).
- [ ] Cleanup: delete consumed files from `changes/`.
- [ ] Local smoke test: hand-craft a couple of change files in a scratch dir,
      run `--dry-run`, verify the planned bumps, PR body, and deletions.

---

## Phase 3 — Workflow 1: `update-release-pr.yml`

- [ ] Create `.github/workflows/update-release-pr.yml`.
- [ ] Trigger on `push` to `main`; add `paths: ['changes/**']` guard so it
      no-ops on unrelated pushes.
- [ ] Permissions: `contents: write`, `pull-requests: write`.
- [ ] Steps:
  - [ ] Checkout `main` (default shallow checkout — full history isn't needed
        while the project is still in flux).
  - [ ] Set up Python and install release requirements (PyYAML).
  - [ ] Configure `git` user (e.g. `github-actions[bot]`).
  - [ ] Run `scripts/update_release_pr.py`. If it reports "no change files",
        exit the job successfully without further steps.
  - [ ] Create branch `versioning` from current `main` state, commit the
        edits as a single commit, force-push.
  - [ ] Use `gh pr view versioning` to detect existing PR; if missing,
        `gh pr create`; if present, `gh pr edit --body`. Title can be static
        (e.g. "Release: pending changes").
- [ ] Verify the workflow file with `actionlint` (or visual review) before
      merging.

---

## Phase 4 — Workflow 2: `publish-release.yml`

- [ ] Create `.github/workflows/publish-release.yml`.
- [ ] Trigger: `pull_request` `closed`, gated by
      `head.ref == 'versioning' && merged == true`.
- [ ] Permissions: `contents: write` (to push tags).
- [ ] Steps:
  - [ ] Checkout with `fetch-depth: 2` so `HEAD~1..HEAD` works.
  - [ ] Compute changed `*/CHANGELOG.yml` paths via `git diff --name-only
        HEAD~1 HEAD`.
  - [ ] For each, parse the `published` field (use `yq` or a tiny Python
        one-liner; pick one and stay consistent).
  - [ ] Create `<project>-v<version>` annotated tag and push it.
- [ ] Manual sanity check: simulate by running the diff + parse locally
      against a contrived release commit.

---

## Phase 5 — Update existing publish workflows

Both files currently trigger on `tags: "v*"`. They must be updated to listen
for project-prefixed tags and to extract the bare semver via
`docker/metadata-action`'s `type=match`.

Note: `executor` is versioned but not published, so no publish workflow is
created for it. `executor-v*` tags will be pushed by Workflow 2 and simply
have no listener — that's intentional.

- [ ] `.github/workflows/publish.yml`
  - Approach: keep a **single file** for both container projects
    (`orchestrator`, `builder`); split the existing matrix into two jobs, each
    gated on its tag prefix via `if: startsWith(github.ref_name, '<prefix>-v')`.
  - [ ] Update `tags:` trigger list to include both `orchestrator-v*` and
        `builder-v*`.
  - [ ] Replace the matrix with two jobs (`publish-orchestrator`,
        `publish-builder`); each runs only when its prefixed tag is pushed.
  - [ ] In each job's `docker/metadata-action`, replace the `type=semver`
        lines with `type=match,pattern=<project>-v(.*),group=1` plus
        major/minor derivations using the same pattern with `value` overrides.
- [ ] `.github/workflows/publish-webapp.yml`
  - [ ] Update `tags:` filter to `webapp-v*`.
  - [ ] Same `type=match` change for metadata extraction.
- [ ] Confirm published Docker tags remain unprefixed (`1.2.0`, `1.2`, `1`).

---

## Phase 6 — Contributor docs

- [ ] Add a "Releasing / Changelogs" section to `README.md` (or a dedicated
      `docs/releasing.md`) covering: how to write a change file, where it
      goes, what happens when the PR merges, and what the release PR looks
      like.
- [ ] Cross-link from `docs/planning/changeset-automation.md` once
      implementation lands (mark the doc as implemented).
- [ ] Update `TODO.md` / `PLAN.md` if either references release tooling.

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

---

## Out of scope (explicit non-goals)

These are listed in the plan and intentionally not on the checklist:

- CI check that every PR includes a change file.
- npm publishing.
- Markdown CHANGELOG generation.
