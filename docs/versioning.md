# Versioning

Drover ships several independently-versioned projects out of one repository.
Versioning is driven entirely by human-authored YAML files — no `npm`, no
Changesets CLI, no third-party release tool — and runs on two GitHub Actions
workflows plus one Python script.

## Versioned projects

| Project | Path | Published as |
|---|---|---|
| `orchestrator` | `orchestrator/` | GHCR Docker image (`ghcr.io/<owner>/drover`) |
| `builder` | `builder/` | GHCR Docker image (`ghcr.io/<owner>/drover-builder`) |
| `webapp` | `webapp/` | GHCR Docker image (`ghcr.io/<owner>/drover-webapp`) |
| `executor` | `executor/` | Not published — git tags only |

Each project owns a `CHANGELOG.yml` at its top level. That file is the
authoritative version record.

## Lifecycle

```
PR with change file ──► merge to main ──► update-release-pr workflow
                                                  │
                                                  ▼
                                 versioning branch + "Release: pending changes" PR
                                                  │
                                          merge release PR
                                                  │
                                                  ▼
                                       publish-release workflow
                                                  │
                                                  ▼
                                  <project>-v<version> git tags pushed
                                                  │
                                                  ▼
                              publish.yml / publish-webapp.yml build & sign
                                                  │
                                                  ▼
                                  GHCR images tagged 1.2.0 / 1.2 / 1
```

1. **Contributor PR.** Whoever is making the change drops a YAML file under
   `changes/` describing which project(s) the PR affects and at what bump
   level. Multiple files can sit there at once — one per in-flight feature.
2. **Merge to `main`.** The `update-release-pr` workflow fires, reads every
   change file, applies the bumps to each affected `CHANGELOG.yml`, deletes
   the consumed change files, and force-pushes the result as a single commit
   to the `versioning` branch. It then creates or updates a "Release:
   pending changes" PR whose body summarises every pending bump grouped by
   project.
3. **Merge the release PR.** The `publish-release` workflow diffs the merge
   commit against its parent, finds every `*/CHANGELOG.yml` that changed,
   reads each new `published` value, and pushes a git tag of the form
   `<project>-v<version>` (e.g. `orchestrator-v0.2.0`).
4. **Publish.** Each prefixed tag is the trigger for the existing publish
   workflow. The captured Docker tags are unprefixed (`0.2.0`, `0.2`, `0`,
   plus `sha-<short>`) — only the git tags carry the project prefix.

A PR that doesn't include a change file is a no-op for releases.

## Change file format

Files live under `changes/` and end in `.yaml` or `.yml`. Filename is up to
the contributor; convention is to use the branch or feature name to reduce
merge collisions (e.g. `fix-auth-timeout.yaml`).

Each file is a YAML list. One file may cover multiple projects:

```yaml
- project: orchestrator
  bump: minor
  description: |
    Refactored job queue to support parallel execution across agents.
- project: builder
  bump: patch
  description: |
    Updated base image to address CVE-2026-1234.
```

| Field | Required | Notes |
|---|---|---|
| `project` | yes | Must match a top-level directory containing a `CHANGELOG.yml`. |
| `bump` | yes | One of `major`, `minor`, `patch`. |
| `description` | yes | Free-form Markdown. Use a `\|` block scalar for multi-line text. |

### Versioning rules

- **Semver, no prereleases.** Versions are `MAJOR.MINOR.PATCH`, starting at
  `0.0.0`. There is no current support for `-rc.1` or similar suffixes.
- **Highest bump wins per project.** If multiple change files affect the same
  project — possibly from different in-flight PRs that all merge before the
  release PR is — the script picks the highest bump (`major` > `minor` >
  `patch`) and rolls every entry into one new version under that bump. All
  individual descriptions are preserved in the changelog.
- **Within a single release**, each project moves at most one version
  forward. The release PR is the unit of release; it doesn't matter how many
  individual change files contributed to it.

## Per-project `CHANGELOG.yml`

Every versioned project has one. Created automatically the first time you
add a project (see "Adding a new project" below); seeded at `0.0.0` with an
empty history.

```yaml
published: "0.1.0"
changes:
  - version: "0.1.0"
    date: "2026-05-09"
    entries:
      - bump: minor
        description: |
          Refactored job queue to support parallel execution across agents.
  - version: "0.0.1"
    date: "2026-04-01"
    entries:
      - bump: patch
        description: |
          Fixed crash on malformed input in the executor loop.
```

`published` is the authoritative current version. The `changes` list is
newest-first. Dates are UTC at the moment the release PR is built.

This file is YAML rather than Markdown so downstream tooling (release notes,
status pages, the webapp itself) can consume it programmatically. Render to
Markdown on demand if a human wants to read it.

## Tag scheme

Git tags carry the project prefix; Docker tags do not.

| What | Format | Example |
|---|---|---|
| Git tag | `<project>-v<MAJOR.MINOR.PATCH>` | `orchestrator-v0.2.0` |
| Docker full tag | `<MAJOR.MINOR.PATCH>` | `0.2.0` |
| Docker minor tag | `<MAJOR.MINOR>` | `0.2` |
| Docker major tag | `<MAJOR>` | `0` |
| Docker SHA tag | `sha-<short>` | `sha-1a2b3c4` |

The git-tag prefix exists so each project has its own tag namespace; the
publish workflows filter by prefix to know which image to build. The Docker
tags stay unprefixed because the project name is already encoded in the
image name (`drover` vs `drover-builder` vs `drover-webapp`).

`executor` produces a git tag (`executor-v<version>`) but no Docker image
— there's no listener for `executor-v*` and that's intentional. Its tags
exist purely as a release record.

## Workflows

| Workflow | Trigger | Job |
|---|---|---|
| `update-release-pr.yml` | `push` to `main` (paths-filtered to `changes/**`, the script, and the workflow itself) | Run the script, force-push `versioning`, create or update the release PR. |
| `publish-release.yml` | `pull_request` `closed` where the head ref is `versioning` and `merged == true` | Diff the merge commit, push `<project>-v<version>` tags. |
| `publish.yml` | `push` to `orchestrator-v*` or `builder-v*` tags | Build, sign with cosign, push to GHCR. Two prefix-gated jobs in one file. |
| `publish-webapp.yml` | `push` to `webapp-v*` tags | Same shape as `publish.yml`, single job. |

The script lives at `scripts/update_release_pr.py` and only mutates files on
disk plus emits the release-PR body to stdout — git operations are entirely
in the workflow YAML.

## Adding a new project

1. Create the project's top-level directory with whatever code it contains.
2. Add `<project>/CHANGELOG.yml`:
   ```yaml
   published: "0.0.0"
   changes: []
   ```
3. Decide whether the project is published as a Docker image. If yes, add a
   prefix-gated job to `publish.yml` (or a new dedicated workflow file)
   matching the existing `publish-orchestrator` / `publish-builder`
   templates: trigger on `<project>-v*`, three `type=match` patterns for the
   metadata extraction, cosign sign step. If no (like `executor`), do
   nothing else — the tags will fire harmlessly with no listener.

Until step 2 lands on `main`, change files referencing the new project will
fail validation in the script — that's intentional, it catches typos.

## Edge cases

- **No change files when a PR merges.** The workflow exits cleanly without
  touching the `versioning` branch or the release PR.
- **`changes/` exists but the release PR is already open.** Each new merge
  to `main` rewrites the `versioning` branch with all currently-pending
  bumps and updates the open PR's body. The PR always has exactly one
  commit relative to `main`.
- **Two PRs merge nearly simultaneously.** A concurrency group on
  `update-release-pr.yml` serializes the runs, so the second one always
  sees the first's results before computing the next state.
- **A tag already exists when `publish-release` runs.** Skipped without
  error — the workflow is idempotent, so re-running on the same merge
  commit is safe.
- **A change file references a project with no `CHANGELOG.yml`.** The
  script aborts with the offending path and project name; nothing is
  modified or pushed.
