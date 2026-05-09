# Change files

Drop a YAML file in this directory as part of any PR that needs a version bump
in one or more projects. The release automation consumes these files when the
PR merges and turns them into per-project `CHANGELOG.yml` updates plus a
release PR.

See [`docs/planning/changeset-automation.md`](../docs/planning/changeset-automation.md)
for the full design.

## Filename

Anything ending in `.yaml` or `.yml`. Convention is to name it after the branch
or feature so two PRs don't collide on the same filename — e.g.
`fix-auth-timeout.yaml`.

## Format

A single file is a YAML list. Each entry describes one project's bump and the
human-readable description that will end up in that project's changelog.

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

### Fields

| Field | Required | Notes |
|---|---|---|
| `project` | yes | Must match a top-level directory containing a `CHANGELOG.yml` (currently `builder`, `executor`, `orchestrator`, `webapp`). |
| `bump` | yes | One of `major`, `minor`, `patch`. |
| `description` | yes | Free-form Markdown. Use a `\|` block scalar for multi-line text. |

## What happens when the PR merges

1. The `update-release-pr` workflow reads every file under `changes/`.
2. Entries are grouped by project; the highest bump wins per project.
3. Each affected `<project>/CHANGELOG.yml` gets a new entry prepended and its
   `published` field updated.
4. The consumed change files are deleted.
5. All of the above is committed to the `versioning` branch and surfaced as a
   single release PR. Merging that PR pushes per-project tags
   (`<project>-v<version>`) which trigger the existing Docker publish
   workflows.

## What if I don't need a version bump?

Skip the change file. PRs without one are a no-op for the release workflow.
