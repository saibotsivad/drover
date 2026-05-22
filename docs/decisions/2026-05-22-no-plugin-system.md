# ADR: No plugin system in Drover core

**Date:** 2026-05-22
**Status:** Accepted

## Context

Two RFCs were drafted exploring a plugin system for the executor: one proposing
a generic initializer protocol that plugins could hook into, and a concrete
first plugin that would run `git clone` before the `ready` signal. The plugin
model was attractive because it kept the core "git-agnostic" while letting
startup features evolve as separate packages.

## Decision

We will not add a plugin system to Drover, and we will not ship a git-clone
startup plugin. Drover's goal is to be a simple, well-designed primitive that
operators build on top of — not a platform that accumulates optional features.
Any setup that can be done in a startup script (cloning a repo, running
`apt-get`, sourcing a config file) should be done there; enlarging Drover's
API surface to formalize that work as plugins adds protocol complexity,
versioning obligations, and maintenance burden without meaningfully improving
what operators can already do today.

## Consequences

- The executor wire protocol remains unchanged; no `initializing` or
  `init_failed` message types are added.
- Operators who need a git checkout before a container is useful should handle
  it in their own startup logic outside of Drover.
- Future proposals for startup features should demonstrate why a startup script
  cannot meet the need before a core change is considered.
