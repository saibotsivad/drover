# ADR: Single Go module under `cli/`, `internal/`-only packages

**Date:** 2026-05-24
**Status:** Accepted

## Context

The repository hosts several independently-versioned projects. The CLI is
the only one written in Go, and it ships a single binary with no library to
publish. The layout options (single module vs. workspace, `internal/` vs.
`pkg/`, nested vs. flat) are weighed in
[`docs/planning/drover-cli-go-implementation.md`](../planning/drover-cli-go-implementation.md).
This ADR records the durable outcome.

## Decision

The CLI is one Go module rooted at `cli/` (module path
`github.com/saibotsivad/drover/cli`). Everything that is not the entry point
lives under `internal/`; `cmd/drover/main.go` only wires the root command.
There is no `pkg/` and no workspace.

## Reasoning

- **Single module.** One binary, no public library — a single `go.mod` is
  the simplest thing that works and matches every other small Go CLI repo.
  It needs no Go workspace despite living in a polyglot monorepo.
- **`internal/` is compiler-enforced.** Nothing outside the `cli` module can
  import these packages, so the HTTP client cannot be mistaken for a public
  SDK. Promoting code to a public `pkg/` later would be a deliberate,
  reviewable step.
- **One package per concern.** `api`, `ws`, `commands`, `output`, `wait`,
  `config`, and `version` each do one thing, keeping per-package testing and
  ownership clear.

## Consequences

- A future public Go client requires an explicit move into `pkg/`, not an
  accidental import.
- New subcommands are added as files in `internal/commands/` and registered
  in `root.go`; the contributor flow is documented in
  [`cli/README.md`](../../cli/README.md).
