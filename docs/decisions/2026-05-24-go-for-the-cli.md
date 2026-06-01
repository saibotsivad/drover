# ADR: Use Go for the Drover CLI

**Date:** 2026-05-24
**Status:** Accepted

## Context

Drover needs a command-line client for the orchestrator REST API so
operators can list images, manage workers, and run exec commands
without hand-writing HTTP requests. The rest of the repository
(orchestrator, executor, release tooling) is Python.

This ADR records the durable outcome of the language evaluation (language
alternatives, dependency choices, build and release pipeline).

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
(equally small binaries) were both considered and rejected — Python for its
runtime-management burden and more awkward WebSocket/signal handling, Rust
for its heavier cross-compile and release tooling and the team's lesser
experience with it.

## Consequences

- The CLI is the repository's only Go module.
- The team accepts a new language in the codebase. The review and onboarding
  risk is mitigated by a tiny dependency set, one-thing-per-file structure,
  and generous package comments.
