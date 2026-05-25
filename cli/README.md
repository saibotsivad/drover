# drover CLI

A single-binary command-line client for the Drover orchestrator. Lets you
list images, launch and manage micro-containers, and run commands in them
without hand-writing HTTP requests.

This README is the **contributor** quickstart: how to build, test, and lint
locally, where each package lives, and how to add a subcommand. For other
audiences:

- **End-user usage** (commands, flags, JSON output, exit codes): see
  [`docs/cli.md`](../docs/cli.md).
- **Installation**: see the release flow in
  [`docs/releases.md`](../docs/releases.md). For end users it's a single
  `curl … | sh`.
- **Design & rationale**: the CLI architecture decision records —
  [Use Go for the CLI](../docs/decisions/2026-05-24-go-for-the-cli.md),
  [Cobra as the CLI framework](../docs/decisions/2026-05-24-cobra-cli-framework.md),
  and [Single Go module, `internal/`-only packages](../docs/decisions/2026-05-24-single-go-module-internal-packages.md).

## Requirements

The CLI authenticates entirely through two environment variables (no config
file):

```sh
export DROVER_API_URL=https://drover.example.com
export DROVER_API_KEY=sk-...
```

A missing or malformed value fails fast with exit code 2.

## Developing

This is a single Go module rooted at `cli/` (module path
`github.com/saibotsivad/drover/cli`). All logic lives under `internal/`;
`cmd/drover/main.go` only wires the root command.

The [`Makefile`](Makefile) is the contract for the dev loop:

```sh
make build      # build ./bin/drover with version metadata baked in
make test       # go test ./...
make lint       # golangci-lint run
make install    # go install ./cmd/drover
make snapshot   # local cross-compile sanity via GoReleaser
```

`make build` and `make install` bake `Version`, `Commit`, and `Date` into
`internal/version` via `-ldflags`; for local builds these come from
`git describe`. Release builds set them through GoReleaser instead.

```sh
./bin/drover --version
./bin/drover --help
```

CI fails on lint findings, so run `make lint` before pushing.
`.golangci.yaml` holds the enabled linters.

## Layout

```
cli/
├── cmd/drover/main.go          Entry point — wires the root command only.
└── internal/
    ├── api/                    HTTP client over the orchestrator REST API.
    ├── ws/                     WebSocket streaming for exec output.
    ├── commands/               One file per subcommand; root.go wires the tree.
    │   └── testdata/           Golden files for the read-only command tests.
    ├── output/                 JSON stdout/stderr emission + exit-code-aware errors.
    ├── wait/                   Polling loop with a deadline for the lifecycle commands.
    ├── config/                 DROVER_API_URL / DROVER_API_KEY loading and validation.
    └── version/                Build-time version metadata (set via -ldflags).
```

`internal/` is compiler-enforced: nothing outside this module can import it.
If a public Go client is ever wanted, it gets promoted into a sibling `pkg/`
directory deliberately. See
[`docs/decisions/2026-05-24-single-go-module-internal-packages.md`](../docs/decisions/2026-05-24-single-go-module-internal-packages.md).

## Adding a subcommand

1. Add `internal/commands/<name>.go` with a `new<Name>Cmd()` function that
   builds and returns a `*cobra.Command`. Read the env-derived client with
   `clientFromEnv()`, and emit results with `output.PrintJSON`. Lifecycle
   commands (those that block until a terminal state) reuse `runLifecycle`.
2. Register it in `newRootCmd` in `internal/commands/root.go`.
3. Add a `<name>_test.go` driving the command against an `httptest` fake
   orchestrator, asserting on exit code and stdout JSON (golden files under
   `internal/commands/testdata/`). Error paths assert on the stderr JSON
   envelope.

Errors carry their own exit code: return an `*output.Failure` (or let an
`*api.Error` propagate) and `output.PrintError` renders it as a JSON object
on stderr with the right code. The exit-code contract is documented in
[`docs/cli.md`](../docs/cli.md).
