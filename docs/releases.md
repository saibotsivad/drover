# Releases

Drover publishes artifacts to several places: GHCR (containers), PyPI
(libraries, when applicable), and GitHub Releases (small assets and the
manifest). A GitHub Release in this repository is a **manifest of
cross-links**, not a container of artifacts — it records which versions of
each component belong together and where each one was actually published.

For per-component versioning (the `<project>-v<version>` git tags, the
`changes/` workflow, the per-project `CHANGELOG.yml` files), see
[`versioning.md`](./versioning.md). This document covers the umbrella
release layer that sits on top.

## Umbrella version scheme

Drover releases use **CalVer with an in-month increment**:

```
v$YEAR.$MONTH-$INCREMENT
```

| Part | Meaning |
|---|---|
| `$YEAR` | Four-digit year, UTC at release time |
| `$MONTH` | One- or two-digit month, no zero padding (`5`, not `05`) |
| `$INCREMENT` | One-based counter, resets at the start of each month |

Examples: `v2026.5-1`, `v2026.5-2`, `v2026.6-1`.

The increment is computed by inspecting the latest existing release: if the
latest release's year and month match the current UTC year and month, the
new release's increment is that release's increment plus one; otherwise it
starts at `1`. This means multiple releases per day are supported by design.

The umbrella version is **not semver** and does not convey API stability.
Component semver continues to do that job.

## What a release contains

Every Drover release has the same shape:

**Always-present assets (small, stable URLs):**

| Asset | Purpose |
|---|---|
| `manifest.yaml` | The full cross-link manifest. Source of truth for which component versions belong to this release. |
| `manifest.yaml.sig` | Cosign signature over `manifest.yaml`. |
| `checksums.txt` | SHA-256 of every asset attached to this release. |
| `install.sh` | CLI installer. Reads `manifest.yaml`, downloads the right CLI binary, verifies its checksum. |

**Body of the release:**

- A human-readable changelog aggregating each component's new entries for
  this release.
- A pinned-versions table (component → version → published location).

**Not in the release:**

- Component binaries (CLI, etc.) are **not** re-uploaded. They live on the
  per-component release where they were originally built; the manifest
  cross-links to them.
- Container images are not re-uploaded. The manifest references them by
  `ghcr.io/...@sha256:...`.

## Manifest schema

`manifest.yaml` is the source of truth for a release. Its shape:

```yaml
drover: v2026.5-3
released: 2026-05-23T18:42:11Z
components:
  orchestrator:
    version: 1.2.4
    image: ghcr.io/saibotsivad/drover:1.2.4
    digest: sha256:abc...
  builder:
    version: 0.4.1
    image: ghcr.io/saibotsivad/drover-builder:0.4.1
    digest: sha256:def...
  webapp:
    version: 2.0.0
    image: ghcr.io/saibotsivad/drover-webapp:2.0.0
    digest: sha256:123...
  executor:
    version: 0.3.1
    pypi: drover-executor==0.3.1
  cli:
    version: 1.0.2
    release: https://github.com/saibotsivad/drover/releases/tag/cli-v1.0.2
    assets:
      linux-amd64:
        url: https://github.com/.../drover-linux-amd64.tar.gz
        sha256: 7f3a...
      linux-arm64:
        url: https://github.com/.../drover-linux-arm64.tar.gz
        sha256: 9c1b...
      darwin-amd64: { url: ..., sha256: ... }
      darwin-arm64: { url: ..., sha256: ... }
      windows-amd64: { url: ..., sha256: ... }
```

Every component present in the repo appears in `components`, whether or not
it changed in this release. Unchanged components carry forward the previous
release's values.

## Commitments

- **Stable install URL.** `https://github.com/saibotsivad/drover/releases/latest/download/install.sh`
  is a permanent link to the latest installer. Likewise `manifest.yaml`,
  `manifest.yaml.sig`, and `checksums.txt`.
- **Signed manifest.** Every release's `manifest.yaml` is cosign-signed
  (keyless OIDC). A consumer can verify it before trusting any of the
  cross-links inside.
- **Pinned by digest.** Container references in the manifest always include
  a `sha256:` digest. Consumers can pull by digest for byte-identical
  reproducibility, independent of any mutable tag.
- **Backport-safe `latest`.** When a release is published for an older
  Drover version (e.g. a security backport), the release workflow sets
  `make_latest=false` on it so `releases/latest` continues to point at the
  most recent forward release.
- **Per-component releases are untouched.** Per-project git tags, per-project
  CHANGELOG.yml, and GHCR publish jobs continue to work exactly as described
  in [`versioning.md`](./versioning.md). The umbrella release runs after
  them, consuming their outputs.

## Lifecycle

```mermaid
flowchart TD
    rp["Release PR merged"] --> tp(["push-tag.yml"])
    tp --> pti["push <project>-v<version> tags"]
    tp --> pub["publish-image.yml (per affected project)"]
    pub --> ur(["umbrella-release.yml"])
    ur --> calc["compute v$YEAR.$MONTH-$INCREMENT"]
    ur --> mani["assemble manifest.yaml"]
    ur --> sign["cosign sign-blob manifest.yaml"]
    ur --> ghr["create GitHub Release with assets"]
```

The umbrella release is the last step of the existing release flow. It
runs only after every per-component publish job in the same release has
succeeded; if any of them fails, no umbrella release is created.

## Installation

End users install the CLI with:

```sh
curl -fsSL https://github.com/saibotsivad/drover/releases/latest/download/install.sh | sh
```

`install.sh`:

1. Detects OS and arch.
2. Downloads `manifest.yaml` from the same release it was downloaded from.
3. Verifies `manifest.yaml.sig` against the published cosign identity.
4. Reads `components.cli.assets[<os>-<arch>]` to find the binary URL and SHA-256.
5. Downloads the binary, verifies its checksum, extracts it, and installs to
   `/usr/local/bin/drover` (or `$DROVER_INSTALL_DIR`).

Container operators pin to a Drover version by reading the manifest:

```sh
curl -sL https://github.com/saibotsivad/drover/releases/download/v2026.5-3/manifest.yaml
```

…and pulling each image by its digest.

## Re-running a release

A `workflow_dispatch` entrypoint on `umbrella-release.yml` lets an operator
regenerate the manifest for a specific Drover version without re-running
the per-component publishes. This is the recovery path for cases where the
umbrella step failed after the component publishes succeeded.
