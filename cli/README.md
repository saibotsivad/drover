# drover CLI

A single-binary command-line client for the Drover orchestrator. Lets you
list images, launch and manage micro-containers, and run commands in them
without hand-writing HTTP requests.

- **User-facing behaviour** (commands, flags, JSON output): see
  [`docs/cli.md`](../docs/cli.md) *(lands with the docs phase)*.
- **Installation**: see the release flow in
  [`docs/releases.md`](../docs/releases.md). For end users it's a single
  `curl … | sh`.
- **Design & build plan**: [`PLAN.md`](../PLAN.md) and
  [`docs/planning/drover-cli-go-implementation.md`](../docs/planning/drover-cli-go-implementation.md).

## Requirements

The CLI authenticates entirely through two environment variables (no config
file):

```sh
export DROVER_API_URL=https://drover.example.com
export DROVER_API_KEY=sk-...
```

## Developing

This is a single Go module rooted at `cli/`. All logic lives under
`internal/`; `cmd/drover/main.go` only wires the root command.

```sh
make build      # build ./bin/drover with version metadata baked in
make test       # go test ./...
make lint       # golangci-lint run
make install    # go install ./cmd/drover
make snapshot   # local cross-compile sanity via GoReleaser
```

```sh
./bin/drover --version
./bin/drover --help
```

## Layout

```
cli/
├── cmd/drover/main.go      Entry point — wires the root command only.
├── internal/
│   ├── api/                HTTP client over the orchestrator REST API.
│   ├── ws/                 WebSocket streaming for exec output.
│   ├── commands/           One file per subcommand; root.go wires the tree.
│   ├── output/             JSON stdout/stderr emission helpers.
│   ├── wait/               Polling loop for the lifecycle commands.
│   ├── config/             DROVER_API_URL / DROVER_API_KEY loading.
│   └── version/            Build-time version metadata (set via -ldflags).
└── testdata/               Golden files and recorded WebSocket fixtures.
```

`internal/` is compiler-enforced: nothing outside this module can import it.

## Adding a subcommand

1. Add `internal/commands/<name>.go` that builds and returns a
   `*cobra.Command`.
2. Register it in `internal/commands/root.go`.
3. Add a `<name>_test.go` driving it against an `httptest` fake orchestrator.
