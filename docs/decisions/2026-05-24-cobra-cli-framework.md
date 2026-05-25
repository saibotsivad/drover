# ADR: Cobra as the CLI framework

**Date:** 2026-05-24
**Status:** Accepted

## Context

The Drover CLI needs subcommand routing, flag parsing, help text, and a
`--version` flag. It also needs to forward everything after a `--` separator
verbatim to the orchestrator for `drover exec <id> -- <cmd...>`.

stdlib `flag` (smaller footprint) and `urfave/cli` (lighter API) were both
considered and rejected; this ADR records the durable outcome.

## Decision

The CLI uses [`github.com/spf13/cobra`](https://github.com/spf13/cobra) for
command parsing. It is the only Cobra-ecosystem dependency — no Viper, no
pflag extensions.

## Reasoning

- **`--` passthrough.** `exec` relies on Cobra's `ArgsLenAtDash()` to
  forward the command string verbatim while still rejecting unknown flags
  *before* the separator. Reimplementing this on stdlib `flag` costs more
  than the dependency saves.
- **Familiarity.** Cobra powers `kubectl`, `docker`, and `gh`, so a new
  reader is most likely to have seen it before. `urfave/cli` is lighter but
  less recognisable.
- **Unknown flags are a hard error.** Cobra's underlying `pflag` rejects
  unrecognised flags by default; the CLI keeps that default everywhere so a
  typo in a script fails loudly.

## Consequences

- Each subcommand is a `*cobra.Command` built in its own file under
  `internal/commands/`, registered in `root.go`.
- No config-file or profile machinery is added (the CLI is env-var-only);
  Cobra's role is confined to argument parsing and help.
