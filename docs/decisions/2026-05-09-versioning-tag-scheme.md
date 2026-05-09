# ADR: Project-prefixed git tags, unprefixed Docker tags

**Date:** 2026-05-09
**Status:** Accepted

## Context

Drover ships four independently-versioned projects from one repository:
`orchestrator`, `builder`, `webapp`, and `executor`. The first three are
published as Docker images on GHCR; `executor` is versioned but not
published. Each project needs its own version stream so a fix in one
doesn't drag the others along.

Two namespaces have to absorb that requirement, and they have different
shapes:

- **Git tags** are a single per-repo namespace. A tag like `v1.2.3`
  can't say which project it refers to.
- **Docker tags** are scoped per image. The image name (`drover`,
  `drover-builder`, `drover-webapp`) already identifies the project.

Releases are driven automatically: merging the `versioning` PR pushes a
tag per project whose `CHANGELOG.yml` changed, and that tag push triggers
the corresponding Docker build.

## Decision

Git tags carry a project prefix; Docker tags do not.

| Surface | Format | Example |
|---|---|---|
| Git tag | `<project>-v<MAJOR.MINOR.PATCH>` | `orchestrator-v0.2.0` |
| Docker tag | `<MAJOR.MINOR.PATCH>` / `<MAJOR.MINOR>` / `<MAJOR>` / `sha-<short>` | `0.2.0` / `0.2` / `0` / `sha-1a2b3c4` |

The publish workflow extracts the bare semver from a prefixed git tag
via `docker/metadata-action`'s `type=match` (capturing group 1 of a
regex anchored to `<project>-v(\d+\.\d+\.\d+)`). On `main` branch
pushes the version regex matches nothing, so only `sha-<short>` is
emitted.

## Reasoning

### The two namespaces have different shapes

The git tag list is one stream per repo. Without a prefix, two projects
both wanting `v1.0.0` would collide. Prefixing is mandatory there, not
stylistic.

The registry's tag list is one stream *per image*. Repeating the project
name inside a Docker tag (`drover-builder:builder-1.2.0`) names the
project twice and breaks any tooling that sorts or matches semver
strings — deployment systems, dependabot-style updaters, manifest
inspectors. Keeping Docker tags unprefixed matches the convention every
other public image follows.

### Pinned shorthand tags stay safe by construction

Consumers pinning `:1.2` or `:1` expect a real release line, not a
pre-release commit. The version-extracting `type=match` patterns are
anchored to the prefixed tag format, so a branch push can't promote a
SHA-only build to a floating shorthand — the regex literally cannot
match a branch ref. This safety is structural, not conventional, so it
can't be regressed by relabelling.

### Automation flows from the prefix

Each publish job in `publish.yml` gates on `startsWith(github.ref,
'refs/tags/<project>-v')`, so a single tag push triggers exactly one
build. The prefix is the routing mechanism: git uses it to disambiguate
projects, the workflow uses it as a job selector, and the regex strips
it before it reaches the Docker registry.

## Consequences

- `executor` participates uniformly: it gets `executor-v<version>` tags
  with no listener and no image. If it ever becomes a published
  artifact, only a new publish job is needed; the tag scheme is
  unchanged.
- Adding a future container project means a new prefix-gated job in
  `publish.yml` and a new flag in `detect-changes`. No registry-side
  change, no rename of existing tags.
- Two surfaces, two audiences: `git tag --list` shows a release history
  grouped by project; `docker pull` lines show clean semver. Each stays
  legible to its own users.
- The full lifecycle, the change-file format, and the workflow file
  layout are documented in [`docs/versioning.md`](../versioning.md).
