# ADR: Use Go for the Drover CLI

**Date:** 2026-05-24
**Status:** Accepted

## Context

Drover needs a command-line client for the orchestrator REST API so
operators can list images, manage micro-containers, and run exec commands
without hand-writing HTTP requests. The rest of the repository
(orchestrator, executor, release tooling) is Python.

The full evaluation — language alternatives, dependency choices, build and
release pipeline — lives in
[`docs/planning/drover-cli-go-implementation.md`](../planning/drover-cli-go-implementation.md).
This ADR records only the durable outcome.

## Decision

The Drover CLI is written in Go, distributed as a single statically-linked
binary (`CGO_ENABLED=0`) across linux/macOS/Windows on amd64 and arm64.

## Reasoning

- **Single-binary distribution.** Go cross-compiles to a self-contained
  executable, so end users install with `curl … | sh` or a manual download
  rather than managing a Python runtime.
- **WebSocket streaming and signal handling.** Clean Ctrl-C handling and
  exit-code propagation over a streamed exec connection are more
  straightforward in Go (context cancellation) than in Python.
- **Release tooling.** GoReleaser gives cross-compilation, archives,
  checksums, and signing out of the box, slotting into the repo's existing
  change-file → version-tag → publish flow.

Python (team familiarity, type sharing with the orchestrator) and Rust
(equally small binaries) were both considered and rejected; see the planning
doc's "Alternatives Considered" section.

## Consequences

- The CLI is the repository's only Go module.
- The team accepts a new language in the codebase; the planning doc's risk
  section covers the review and onboarding mitigations (tiny dependency set,
  one-thing-per-file structure, generous package comments).
