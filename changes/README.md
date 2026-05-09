# Change files

Drop a YAML file in this directory as part of any PR that should bump a
project's version. The release workflow consumes these files when the PR
merges. Skip the file if your PR doesn't need a release.

See [`docs/versioning.md`](../docs/versioning.md) for the full lifecycle.

## Quick reference

Filename: anything ending in `.yaml` or `.yml`. Convention is to name it
after the branch or feature (e.g. `fix-auth-timeout.yaml`) so two PRs don't
collide.

Format — copy and edit:

```yaml
- project: orchestrator
  bump: minor
  description: |
    Short, human-readable summary of the change.
```

Multiple projects in one file is fine:

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
| `project` | yes | One of `builder`, `executor`, `orchestrator`, `webapp`. |
| `bump` | yes | One of `major`, `minor`, `patch`. |
| `description` | yes | Free-form Markdown. Use `\|` for multi-line. |
