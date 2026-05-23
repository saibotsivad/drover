# ADR: GitHub Release is a cross-link manifest, not an artifact

**Date:** 2026-05-23
**Status:** Accepted

## Context

Drover publishes from a single repository to several places at once: containers
to GHCR (`orchestrator`, `builder`, `webapp`), a CLI binary that needs a
stable download URL, an `executor` library intended for PyPI, and likely more
distribution channels later. Each channel has its own versioning and its own
"latest" pointer. GitHub Releases is a single per-repo namespace with one
`latest` link, which doesn't naturally fit a monorepo with multiple
independently-versioned, externally-published artifacts.

## Decision

Each GitHub Release for this repository is a **manifest of cross-links**, not
a container for the artifacts themselves. The release describes which
component versions belong together, where each one was actually published
(GHCR, PyPI, a per-component release, etc.), and includes integrity metadata
(digests, checksums) so a consumer can verify the combination it pulled.

A release contains:

- A signed `manifest.yaml` listing every component, its version, and the
  external location where the artifact lives (image reference + digest, PyPI
  spec, asset URL, etc.).
- Small always-present assets that need a stable URL: `install.sh`,
  `checksums.txt`, `manifest.yaml.sig`.
- No re-uploaded large binaries. Component binaries (e.g. the CLI) live on
  the per-component release where they were built; the manifest cross-links
  to them.

The umbrella version is **CalVer with an in-month increment**:
`v$YEAR.$MONTH-$INCREMENT` (e.g. `v2026.05-3`). The increment resets each
month and is computed by inspecting the most recent existing release.

## Consequences

- `releases/latest` always points at a coherent, tested combination of
  components. Pinning to a Drover release is meaningful in a way that
  pinning to a per-component tag is not.
- The umbrella version does not pretend to convey API stability. Component
  semver continues to do that job; the manifest records which versions were
  combined.
- The existing per-project versioning scheme (per-project `CHANGELOG.yml`,
  `<project>-v<version>` git tags, GHCR publishes) is unchanged. The manifest
  layer sits on top and consumes their outputs.
- A consumer can verify the full chain: signature on `manifest.yaml` →
  digests for each image → cosign signature on each image.
- Release notes live on the umbrella release. Per-project changelogs remain
  the authoritative source for per-component history.

## Related

- [`docs/releases.md`](../releases.md) — release strategy and commitments.
- [`docs/versioning.md`](../versioning.md) — per-component versioning.
- [`docs/decisions/2026-05-09-versioning-tag-scheme.md`](./2026-05-09-versioning-tag-scheme.md) — the per-component tag scheme this builds on.
