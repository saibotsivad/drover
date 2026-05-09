# Changeset Automation

> A custom release-PR workflow using human-authored change files to drive independent versioning and CHANGELOG generation across multiple containers, without relying on npm tooling.

---

## Goal

After this is implemented, a contributor can:

1. Drop one or more YAML files into `changes/` as part of their PR, describing which projects are affected and how.
2. When that PR merges, a GitHub Workflow automatically creates or updates a single "release PR" that bumps versions, updates per-project `CHANGELOG.yml` files, and removes the consumed change files.
3. When the release PR is merged, a second Workflow reads which `CHANGELOG.yml` files changed, creates the appropriate git tags, and the existing Docker publish workflows handle the rest.

No npm, no Changesets CLI, no third-party release tool.

---

## Why custom

Several existing tools were evaluated:

| Tool | File-driven? | Monorepo independent versioning? | Non-npm? |
|---|---|---|---|
| Changesets | Yes | Yes (best-in-class) | No — shoehorned via private package.json |
| Towncrier | Yes | No — single project only | Yes (Python-native) |
| Changie | Yes | Partial — projects feature is bolted on | Yes |
| Knope | Yes | Yes | Yes |

Changesets has the right workflow model but is fundamentally npm-centric. Knope is the closest principled alternative. Given that the core logic is simple and bounded (~150 lines of Python), a custom script avoids any ongoing awkwardness with tools designed for different ecosystems, stays fully transparent, and fits naturally into the existing Python codebase.

---

## File structure

### Change files — `changes/*.yaml`

Added by contributors as part of a PR. Any filename is fine; convention is to name it after the branch or feature to reduce merge conflicts (e.g. `fix-auth-timeout.yaml`).

A single file may cover multiple projects for cross-cutting changes:

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

Valid `bump` values: `major`, `minor`, `patch`.

### Per-project changelog — `<project>/CHANGELOG.yml`

One file per project directory. Serves as both the version record and the full release history. Created automatically on first use starting at `0.0.0`.

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

`published` is the authoritative current version for that project. The `changes` list is newest-first.

---

## Conventions

**Project name = directory name.** The `project` field in a change file must match the project's top-level directory name exactly (`orchestrator`, `builder`, `webapp`). The script writes `CHANGELOG.yml` to `./<project>/CHANGELOG.yml`.

**Semver precedence.** If multiple change files affect the same project, the highest bump wins for the version calculation (`major > minor > patch`). All entries are included in the CHANGELOG under that single new version.

**No change files = no-op.** If `changes/` is empty when a PR merges, Workflow 1 exits cleanly without touching the release PR.

---

## Workflows

### Workflow 1 — Update release PR (`update-release-pr.yml`)

**Trigger:** `push` to `main`.

**Steps:**
1. Read all YAML files in `changes/`.
2. Group entries by project; determine new version for each (highest bump applied to current `published`).
3. Prepend a new entry to each affected `<project>/CHANGELOG.yml` and update `published`.
4. Delete all consumed files from `changes/`.
5. Force-push to the fixed branch `release/next` as a single commit (so the PR always has exactly one commit).
6. Create the PR if it doesn't exist; update its body if it does. PR body is generated Markdown summarising all pending changes grouped by project, e.g.:

```markdown
## orchestrator — 0.1.0 → 0.2.0 (minor)

- Refactored job queue to support parallel execution across agents.

## builder — 0.0.0 → 0.0.1 (patch)

- Updated base image to address CVE-2026-1234.
```

### Workflow 2 — Publish on release PR merge (`publish-release.yml`)

**Trigger:** `pull_request` event, type `closed`, where `github.event.pull_request.head.ref == 'release/next'` and `github.event.pull_request.merged == true`.

**Steps:**
1. Diff `HEAD~1..HEAD` to find which `*/CHANGELOG.yml` files changed.
2. Read the new `published` value from each changed file.
3. Create a git tag per project: `<project>-v<version>` (e.g. `orchestrator-v0.2.0`).
4. Push the tags — the existing Docker publish workflows handle the rest.

---

## Docker publish workflow updates

The existing `publish.yml` and `publish-webapp.yml` both trigger on `push: tags: "v*"`. These need updating to:

- Trigger on their project-specific tag prefix (`orchestrator-v*`, `builder-v*`, `webapp-v*`).
- Use `type=match` in `docker/metadata-action` to extract the bare semver from the prefixed tag:

```yaml
tags: |
  type=match,pattern=orchestrator-v(.*),group=1
  type=sha
```

The published Docker image tags remain unprefixed (e.g. `1.2.0`, `1.2`, `1`) — only the git tags carry the project prefix.

---

## Scripts

All script code lives in `/scripts/`. The main script (`scripts/update_release_pr.py`) is invoked by Workflow 1. It uses:

- `PyYAML` for reading/writing YAML files (already available in the Python ecosystem).
- `gh` CLI (pre-installed on GitHub Actions runners) for creating/updating the PR.
- Standard `git` commands for branching and force-pushing.

Workflow 2 is simple enough to be implemented entirely in the workflow YAML using shell and `git diff`.

---

## Out of scope

- Enforcing that every PR includes a change file (no CI check; trust the contributor).
- Any npm publishing.
- Generating Markdown changelog files (YAML only; humans or downstream tooling can render these).
