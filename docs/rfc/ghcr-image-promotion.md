# RFC: Promote tested GHCR images instead of rebuilding at release

**Date:** 2026-05-25
**Status:** Draft — seeking team buy-in

## Summary

Today the release flow **rebuilds** each image from source when a version is
cut. This RFC proposes that we instead **promote** an already-published,
already-tested image: run e2e against the exact image in GHCR, then on release
just point the version tags (`1.2.3`, `1.2`, `1`, `latest`) at that same image
digest — no rebuild. Because OCI images are content-addressed, the released
artifact becomes provably byte-for-byte identical to the one we tested.

This is a registry-mechanics win that's cheap on the GHCR side, but it requires
two structural changes to our pipeline and one genuine design decision. The
goal of this RFC is to get agreement on the direction (and which shape) before
anyone writes a plan.

## Motivation

Our strongest current guarantee is "we built the release from the same commit
we tested." That is not the same as "we shipped the bits we tested." Two builds
of the same Dockerfile at different times can differ: a moving base image tag,
a transitively-updated apt/pip package, build-time timestamps, a non-pinned
layer. The gap is small but real, and it sits exactly at the step we care most
about — the thing users pull.

If e2e runs against the actual published image and the release is that same
image (same `sha256:` digest), the gap closes by construction. There is no
"release build" to diverge.

## Background: how it works today

- **`publish.yml`** builds SHA-tagged images on every push to `main` (via
  `publish-image.yml` with no version, `mark_latest: false`). These are our
  per-commit pre-release images.
- **`push-tag.yml`** is the release entry point. When the `versioning` PR
  merges, it pushes `<project>-v<version>` git tags and calls
  `publish-image.yml` again — this time with a `version` and `mark_latest: true`.
  That call **rebuilds the image from source** with `docker/build-push-action`
  and pushes the semver + `latest` tags.
- **`e2e.yml`** is manual-dispatch only and runs `./e2e/run.sh ci`, which uses
  `e2e/docker-compose.e2e.yml`. That compose file **builds images locally**
  (`build: context: ..`, tagged `drover-*:e2e`). It does **not** pull from
  GHCR. So today nothing tests a published image.

So there are two independent build events (per-commit SHA build, and the
release rebuild), plus a third local build inside e2e — three chances to
diverge.

## Proposal

Adopt an OCI **promotion** model for the three container images
(`drover`, `drover-builder`, `drover-webapp`):

1. e2e pulls and tests the **published GHCR image by digest**, not a local build.
2. On release, the version tags are created by **repointing** to that tested
   digest — no rebuild.

### Why this works (registry mechanics)

GHCR stores OCI images, which are content-addressed by digest. A tag is a
mutable named pointer to a manifest `sha256:…`. "Promoting" = adding new tags
that point at a digest that already exists. No rebuild, no pull, identical bits.
If `1.2.3` and `sha-abc123` resolve to the same digest, they are the same image.

The clean tool is `docker buildx imagetools create`, which operates registry-side
on the manifest and preserves multi-arch manifest lists:

```bash
docker buildx imagetools create \
  --tag ghcr.io/saibotsivad/drover:1.2.3 \
  --tag ghcr.io/saibotsivad/drover:1.2 \
  --tag ghcr.io/saibotsivad/drover:1 \
  --tag ghcr.io/saibotsivad/drover:latest \
  ghcr.io/saibotsivad/drover@sha256:<tested-digest>
```

One invocation creates all the tags our `metadata-action` block emits today.
Equivalent tools: `crane tag`/`crane copy`, `skopeo copy`, `regctl image copy`.
We must **avoid** `docker pull` + `docker tag` + `docker push`, because plain
`pull` resolves a single platform, so re-pushing produces a new single-arch
digest and breaks both the guarantee and multi-arch.

### Two properties that make it airtight

- **Pin e2e by digest, not by the `sha-<gitsha>` tag.** Tags are mutable;
  digests are not. Testing `@sha256:abc` and then promoting `sha256:abc` leaves
  no window where a tag could be overwritten between test and release.
- **Cosign signatures and attestations carry over for free.** `publish-image.yml`
  already signs by digest and attaches provenance. Signatures/attestations
  attach to the digest (as referrers), so adding version tags doesn't disturb
  them — `cosign verify …:1.2.3` resolves to the same digest and still verifies.
  No re-signing required.

## The key design decision

Our release model decouples build from release, so "the tested digest" needs a
definition. SHA images are built per-commit on `main`; the release fires later,
when the `versioning` PR merges — and that merge is itself a new commit on
`main`, triggering yet another SHA build. The team needs to pick which digest
gets promoted:

### Option A — Promote-on-merge (recommended)

`versioning` PR merges → `publish.yml` builds the SHA image for the merge
commit → e2e gates against **that** digest → on green, promote it to the version
tags.

- Pro: strongest, simplest mental model — "what merged is exactly what ships."
- Pro: no digest-threading plumbing; the candidate is always the just-built
  merge-commit image.
- Con: puts the ~20-minute e2e suite on the critical release path.
- Con: requires sequencing e2e before the promote step inside the release flow.

### Option B — Carry-forward a validated digest

Record the digest e2e validated earlier (e.g. the last green `main` build or
the PR head image) and thread it explicitly into `push-tag.yml` to promote.

- Pro: release is fast — no e2e on the critical path.
- Con: more plumbing to carry the digest through the workflows.
- Con: we must separately guarantee the code at release equals the tested code,
  which partially reintroduces the gap we're trying to eliminate.

## What's involved

Roughly, in increasing order of effort:

1. **e2e pulls instead of builds (the real lift).** Today `e2e/run.sh ci` runs
   `compose build` against `e2e/docker-compose.e2e.yml`. To test what ships,
   that compose must reference `ghcr.io/saibotsivad/...@sha256:…` digests
   instead of building locally. Needs a way to inject the candidate digests and
   probably a "build locally" vs "pull published" mode so local dev still works.
2. **Release path retags instead of rebuilds.** Replace the
   `docker/build-push-action` step in the release call to `publish-image.yml`
   with an `imagetools create` promotion of the tested digest. The `digest`
   output that feeds the umbrella manifest keeps the same shape.
3. **Wire up the chosen shape (A or B)** to define and pass the tested digest.

Things that do **not** change / are already fine:

- `prune-ghcr.yml` protects release-tagged versions; promotion creates exactly
  those tags, so pruning still protects them.
- The umbrella release (`umbrella-release.yml`) consumes a digest per component;
  promotion produces a digest just like a build does.
- Cosign verification chain (manifest → image digest → signature) is unchanged.

## The CLI caveat

This only applies to container images. The CLI is GoReleaser binaries published
to a per-component GitHub Release (`cli-v<version>`), not a registry artifact
with a digest to repoint. The closest analog is **checksum continuity**: verify
the tested binary's checksum matches the released one (GoReleaser already emits
`checksums.txt`). That's a strong guarantee but verification-based, not
same-bytes-by-construction. Net: images get the stronger guarantee; the CLI
stays checksum-based.

## Benefits

- The released image is provably the tested image — the build-divergence gap at
  the most important step disappears.
- e2e gains value: it exercises the real published artifact, including its base
  layers and final packaging, not a local rebuild.
- No new infrastructure; uses GHCR's native content-addressing and tooling we
  already have in CI.
- Signatures/attestations and the umbrella manifest are unaffected.

## Risks / open questions

- **e2e on the release path (Option A).** Are we comfortable with ~20 min of
  e2e gating every release? (Mitigations: keep the SHA build cached, parallelize.)
- **Digest provenance (Option B).** If we carry a digest forward, how do we
  prove the release commit's code matches the tested image? This needs a crisp
  rule or it reopens the gap.
- **e2e harness dual-mode.** Local dev must keep building from source;
  CI must pull by digest. The compose/runner needs both paths cleanly.
- **Multi-arch.** If any image becomes a multi-arch manifest list, stick to
  `imagetools`/`crane`/`skopeo` (never `docker tag`).
- **First-release / bootstrap.** The promote path assumes a tested SHA image
  exists; the very first publish of a new image needs a defined behavior.

## Recommendation

Adopt the promotion model, with **Option A (promote-on-merge)** as the default,
because it preserves the guarantee end-to-end with the least plumbing. If the
e2e-on-release-path latency proves unacceptable, fall back to Option B with an
explicit digest-provenance rule. Either way, the CLI continues to ship via
GoReleaser with checksum-based verification.

## Related

- [`.github/workflows/README.md`](../../.github/workflows/README.md) — current CI/CD flow.
- [`.github/workflows/publish-image.yml`](../../.github/workflows/publish-image.yml) — the reusable build/sign workflow this would change.
- [`.github/workflows/push-tag.yml`](../../.github/workflows/push-tag.yml) — release entry point.
- [`docs/full-e2e-suite.md`](../full-e2e-suite.md) — e2e lifecycle.
- [`docs/releases.md`](../releases.md) — release strategy and commitments.
- [`docs/decisions/2026-05-23-github-release-as-manifest.md`](../decisions/2026-05-23-github-release-as-manifest.md) — umbrella manifest model that consumes image digests.
