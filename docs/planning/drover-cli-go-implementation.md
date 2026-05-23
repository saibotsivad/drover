# Drover CLI — Go Implementation

> Draft for team review — not yet adopted. Companion plan to
> [`drover-cli.md`](./drover-cli.md), which defines the user-facing
> behaviour. This document covers only the *how* of building and shipping
> the CLI in Go.

---

## Goal / Desired Outcome

Implement the CLI described in `drover-cli.md` as a single statically-linked
Go binary, distributable across Linux, macOS, and Windows on amd64 and
arm64. A new contributor with no prior Go experience should be able to:

1. Clone the repo, run `make test` inside `cli/`, and have everything pass.
2. Run `make build` and get a working `drover` binary in `cli/bin/`.
3. Read one README and understand where each piece lives.

End users should be able to install with a single command (`curl … | sh`)
or `go install`, and the release process should fit the same change-file →
version-tag → publish flow already used for the orchestrator, builder, and
webapp.

---

## Background

The user-facing CLI contract (commands, flags, JSON output, exec
streaming, polling semantics) is fully specified in
[`drover-cli.md`](./drover-cli.md). That plan is intentionally
language-agnostic.

This plan picks Go and works out the consequences: project layout,
dependencies, tests, build, release, installation, and the matching
CI workflows. It also slots `cli` into the repo's existing
versioning machinery described in [`docs/versioning.md`](../versioning.md).

The team is not deeply familiar with Go, so the bias throughout is toward
the smallest viable surface, idiomatic conventions, and tooling that the
broader Go community already knows. Anything exotic is called out
explicitly so it can be challenged in review.

---

## Proposal

### Folder structure under `/cli`

```
cli/
├── CHANGELOG.yml              Versioning record (consumed by push-tag.yml).
├── README.md                  Quickstart, dev loop, layout map, link to /docs/cli.md.
├── LICENSE                    Copied from repo root if licensing differs per-binary; otherwise omit.
├── go.mod                     Module path: github.com/<owner>/drover/cli
├── go.sum
├── Makefile                   Thin wrapper: test, lint, build, install, release-dry.
├── .goreleaser.yaml           Cross-compile + GitHub Release config.
├── .golangci.yaml             Lint configuration (see "Linting" below).
│
├── cmd/
│   └── drover/
│       └── main.go            Entry point. Wires the root command and calls Execute().
│
├── internal/
│   ├── api/                   HTTP client over the orchestrator REST API.
│   │   ├── client.go          Client struct, request plumbing, auth header.
│   │   ├── containers.go      Create / Get / Stop / Destroy / List bindings.
│   │   ├── images.go          List / Get image bindings.
│   │   ├── execs.go           POST /containers/{id}/execs.
│   │   ├── types.go           Shared request/response structs (mirroring orchestrator models).
│   │   └── errors.go          API error decoding + typed errors (NotFound, Timeout, etc.).
│   │
│   ├── ws/                    WebSocket streaming for exec output.
│   │   └── stream.go          Dial /containers/{id}/ws, filter by command_id, pass frames through.
│   │
│   ├── commands/              One file per CLI subcommand. Each file builds and returns a *cobra.Command.
│   │   ├── root.go            Root command, global flags, version, env-var validation.
│   │   ├── images.go          drover images
│   │   ├── image.go           drover image <name>
│   │   ├── ps.go              drover ps
│   │   ├── start.go           drover start <image-name>
│   │   ├── stop.go            drover stop <container-id>
│   │   ├── destroy.go         drover destroy <container-id>
│   │   └── exec.go            drover exec <container-id> -- <command...>
│   │
│   ├── output/                Stdout/stderr JSON emission helpers.
│   │   └── output.go          PrintJSON(value), PrintError(err) — exit-code aware.
│   │
│   ├── wait/                  Polling loop with deadline (used by start/stop/destroy).
│   │   └── wait.go            Wait(ctx, interval, deadline, fetchFn, doneFn).
│   │
│   ├── config/                Env-var loading and validation.
│   │   └── config.go          Load() reads DROVER_API_URL / DROVER_API_KEY, errors clearly if absent.
│   │
│   └── version/               Build-time version metadata.
│       └── version.go         Vars set via -ldflags during build; exposed by `drover --version`.
│
├── scripts/
│   └── install.sh             curl|sh installer. Detects OS/arch, downloads, verifies checksum.
│
└── testdata/                  Golden files, recorded fixtures.
    ├── ps/                    Example ps responses for table-driven tests.
    └── exec/                  Recorded WS frames for stream tests.
```

Notes on the layout:

- **`cmd/drover/main.go` only wires.** All real logic lives in `internal/`.
  That keeps `main` tiny and uniform with the wider Go ecosystem.
- **`internal/` is enforced by the Go compiler.** Nothing outside the
  `cli` module can import these packages, so there is no risk of someone
  treating the CLI's HTTP client as a public SDK by accident. If we ever
  want a public Go client library, it gets promoted into a sibling
  `pkg/` directory deliberately.
- **One file per subcommand.** Mirrors the command surface in
  `drover-cli.md` so a reader scanning `internal/commands/` sees the same
  list of commands as the user-facing docs.
- **No `pkg/`, no `api/v1/`, no nested module.** Single module, single
  binary. We can grow into a richer layout later if there's demand.

### Go module and language version

- **Module path**: `github.com/<owner>/drover/cli`. Even though the repo
  hosts multiple projects, the CLI is the only Go module, so a `go.mod` at
  `cli/go.mod` works without a workspace.
- **Go version**: pin a single minor version (e.g. `go 1.23` in `go.mod`)
  and use the same version in CI via `actions/setup-go`. Bumping the
  language version is a deliberate change, recorded in `CHANGELOG.yml`.

### Dependencies

Aim for a small, well-known dependency set so a reader unfamiliar with Go
can recognise everything without research.

| Dependency | Purpose | Why this one |
|---|---|---|
| `github.com/spf13/cobra` | CLI framework (subcommands, flags, help) | De facto standard. Powers `kubectl`, `docker`, `gh`. Native support for `--` arg passthrough (needed by `exec`). |
| `github.com/coder/websocket` | WebSocket client | Modern, context-aware, single-file API, zero external deps. `gorilla/websocket` is the older incumbent but is archived. |
| stdlib `net/http` | REST calls | No HTTP client library needed for our surface. |
| stdlib `encoding/json` | JSON marshalling | Same — output is plain JSON objects. |

Explicitly *not* taking on:

- A config-file library (no config file — env vars only, per
  `drover-cli.md`).
- A retry/backoff library (the polling loop is small and bespoke).
- A logging library (CLI writes JSON to stdout/stderr; no structured
  logging needed).
- Viper, pflag-extensions, or anything else from the "Cobra ecosystem"
  beyond Cobra itself.

### Command wiring

A brief sketch only, to anchor the structure — actual code is written during
implementation:

```go
// internal/commands/exec.go
cmd := &cobra.Command{
    Use:   "exec <container-id> -- <command...>",
    Args:  cobra.MinimumNArgs(1),
    RunE:  runExec,
}
cmd.Flags().SetInterspersed(false) // stop flag parsing at `--`
```

The `--` separator is handled by Cobra's `SetInterspersed(false)` plus
`cmd.ArgsLenAtDash()`, which returns the index where `--` appeared.
Anything after is forwarded verbatim to the orchestrator. The bare
`drover exec <id>` form (no `--`) errors with the "interactive exec not
yet supported" message specified in the parent plan.

Repeatable flags (`--env KEY=VALUE`) use `StringArrayVar`, which preserves
order and allows duplicates. KEY=VALUE parsing happens in the command,
not in the flag parser, so error messages can be specific.

### HTTP client

`internal/api/client.go` exposes a single `Client` struct constructed from
`config.Config`. It:

- Adds `Authorization: Bearer <DROVER_API_KEY>` to every request (the
  orchestrator's existing convention, see `orchestrator/auth.py`).
- Decodes error responses into a typed `*APIError` carrying the HTTP
  status and the orchestrator's `detail` field, so commands can decide
  whether to retry, surface, or remap.
- Uses a `context.Context` on every method so polling and exec streams
  can be cancelled (Ctrl-C handling — see below).

No retry logic at the HTTP layer. The only "retry-shaped" behaviour is
the polling loop in `internal/wait`, which is purpose-built for the
`start`/`stop`/`destroy` lifecycle and uses the
`transition_timeout_seconds` value the orchestrator returns.

### WebSocket exec streaming

`internal/ws/stream.go` opens the per-container WebSocket, then loops:

1. Read the next frame (`coder/websocket.Conn.Read`).
2. Decode just enough to inspect `command_id` and `type`.
3. If `command_id` matches, write the raw frame bytes followed by `\n`
   straight to stdout — no re-marshalling.
4. If `type == "status"` and `status == "complete"` for our `command_id`,
   record `exit_code` and return.
5. Otherwise drop the frame.

This matches the "pass-through, no re-shaping" contract in the parent
plan. The CLI never demultiplexes into stdout/stderr; that's the caller's
job via `jq`.

Cancellation is wired through `context.Context` so Ctrl-C closes the
socket cleanly and exits with code 130 (the conventional SIGINT exit
code).

### Output and exit codes

`internal/output/output.go` provides:

- `PrintJSON(any)` — marshals to compact JSON and writes to stdout with a
  trailing newline.
- `PrintError(err)` — marshals to `{"error": "...", "detail": "..."}` on
  stderr and returns the exit code the caller should use.

Exit-code policy:

| Situation | Code |
|---|---|
| Success | 0 |
| `drover exec` — propagated from `exit_code` in the status frame | 0–255 (as reported) |
| Generic API error (4xx/5xx with `detail`) | 1 |
| Missing/invalid `DROVER_API_URL` or `DROVER_API_KEY` | 2 |
| Polling timeout (`start`/`stop`/`destroy`) | 3 |
| `drover start` ended in `error` state | 4 |
| SIGINT | 130 |

These are nailed down here (not in the parent plan) so they're consistent
across commands. They're testable, scriptable, and stable.

### Testing strategy

Three layers, all using stdlib `testing` (no test framework dependency).

1. **Unit tests** colocated with each package
   (`internal/api/containers_test.go`, etc.). Table-driven where the
   shape lends itself. Coverage target: every error branch in
   `internal/api` and `internal/wait`.
2. **HTTP integration tests** using `net/http/httptest.NewServer` to
   stand up a fake orchestrator. Each command file gets a corresponding
   `*_test.go` that exercises the command end-to-end against the fake
   server and asserts on:
   - Exit code
   - stdout JSON shape (goldenfile under `testdata/`)
   - stderr content on error paths
3. **WebSocket integration tests** using `httptest` upgraded with
   `coder/websocket`. Same pattern: pre-recorded frame sequences in
   `testdata/exec/`, assert that the CLI pipes them through unchanged.

Explicitly out of scope for unit tests:
- Real network against a real orchestrator. That's covered by the
  existing `e2e/` suite — the CLI gets a new e2e scenario (see "CI"
  below).
- Cross-platform behaviour. We trust Go's stdlib here; release builds
  exercise cross-compilation, and `install.sh` is tested manually on the
  three target OSes during release validation.

### Linting

`.golangci.yaml` enables a conservative set:

- `gofmt`, `goimports` (formatting)
- `govet`, `staticcheck` (correctness)
- `errcheck` (no silently ignored errors)
- `revive` with default rules (style)

CI fails on lint findings. Pre-commit hooks are not enforced — `make
lint` is the contract.

### Makefile

Single source of truth for the dev loop. Targets:

```
make test       go test ./...
make lint       golangci-lint run
make build      go build -o bin/drover -ldflags="-X .../version.Version=$(git describe)" ./cmd/drover
make install    go install ./cmd/drover
make release    goreleaser release --clean (used only by CI; documented for local dry-runs)
make snapshot   goreleaser release --snapshot --clean (for local cross-compile sanity)
```

`make build` and `make install` set version metadata via `-ldflags`
into `internal/version/version.go` (`Version`, `Commit`, `Date`). For
non-release builds, `git describe --tags --dirty --always` is the source.

### Build & distribution

Use [GoReleaser](https://goreleaser.com) for releases. It's the standard
in the Go community for shipping CLI binaries to GitHub Releases.

`.goreleaser.yaml` produces:

- **Binaries**: linux/amd64, linux/arm64, darwin/amd64, darwin/arm64,
  windows/amd64. Statically linked (`CGO_ENABLED=0`).
- **Archives**: `.tar.gz` for unix, `.zip` for windows, each containing
  the binary, README, and LICENSE.
- **Checksums**: a single `checksums.txt` covering every archive.
- **GitHub Release**: artefacts attached to a release named after the
  pushed tag.
- **Cosign signing**: the checksums file is signed with cosign (keyless,
  using GitHub OIDC) — mirrors what the existing Docker publish does, so
  verification policy can be unified.

No Homebrew tap, no Scoop bucket, no Linux packages in v1. These are
listed as open questions below.

### Installation

Three documented paths:

1. **`install.sh`** (recommended for most users):
   ```sh
   curl -fsSL https://<host>/drover/install.sh | sh
   ```
   The script:
   - Detects OS (`uname -s`) and arch (`uname -m`), normalises to
     GoReleaser's naming.
   - Fetches the latest release tag from the GitHub API.
   - Downloads the matching archive and `checksums.txt`.
   - Verifies sha256.
   - Extracts to `/usr/local/bin/drover` (or `$DROVER_INSTALL_DIR` if
     set), creating the dir if needed and using `sudo` only when the
     target is not writable.
   - Prints the installed version on success.

   The script lives at `cli/scripts/install.sh` in the repo. Where it's
   *served from* is an open question (see below).

2. **`go install`** (for Go-savvy users / CI):
   ```sh
   go install github.com/<owner>/drover/cli/cmd/drover@latest
   ```
   This produces a binary without GoReleaser's version metadata baked
   in, so `drover --version` reports `(devel)` or the tag — acceptable
   for this path.

3. **Manual download** from the GitHub Releases page. Documented as
   fallback for restricted environments.

### CI workflows

Two new workflows, plus a small extension to `push-tag.yml`.

1. **`.github/workflows/cli-test.yml`** — runs on every PR and every push
   to `main`, paths-filtered to `cli/**`.

   Jobs:
   - `lint`: `golangci-lint` (uses `golangci/golangci-lint-action`).
   - `test`: `go test ./...` with `-race -cover`.
   - `build-matrix`: `goreleaser build --snapshot --clean` to confirm
     every target architecture still compiles. No artefacts uploaded.

   Time budget: under 3 minutes total. Caching via `actions/setup-go`'s
   built-in module cache.

2. **`.github/workflows/release-cli.yml`** — reusable workflow
   (`workflow_call`) that runs GoReleaser. Inputs: `version` (the bare
   semver, e.g. `0.2.0`). Permissions: `contents: write` (for the
   release), `id-token: write` (for cosign keyless signing). Called from
   `push-tag.yml` (next).

3. **Extension to `.github/workflows/push-tag.yml`** — add a `cli` case
   to the `scan` step alongside `orchestrator`/`builder`/`webapp`, plus a
   downstream `publish-cli` job that `uses:` `release-cli.yml`. This
   slots the CLI into the existing change-file → versioning-PR →
   tag-push → publish flow with no new release ceremony for
   contributors. The git tag (`cli-v<version>`) is created the same way
   as for every other project.

4. **`e2e/` extension** — add a CLI scenario that drives a real
   orchestrator through the `drover` binary. Initially: `drover ps`,
   `drover start`, `drover exec`, `drover stop`. Reuses the existing
   e2e harness; the CLI binary is built in a setup step. This is the
   single integration point where the CLI talks to a real orchestrator
   in CI.

### Versioning slot

Per `docs/versioning.md`, the CLI becomes a new versioned project. The
table in that doc grows one row:

| Project | Path | Published as |
|---|---|---|
| `cli` | `cli/` | GitHub Release with cross-compiled binaries |

`cli/CHANGELOG.yml` is seeded at `0.0.0` with an empty `changes:` list,
following the "Adding a new project" steps already documented. Change
files reference `project: cli` exactly as they do for other projects.

---

## Alternatives Considered

**Language: Python.** The rest of the repo (orchestrator, executor,
release scripts) is Python. A Python CLI would reuse team knowledge and
could share types with the orchestrator. Rejected because: (a) the team
explicitly wants a single-binary distribution to avoid asking users to
manage a Python runtime; (b) WebSocket streaming with clean Ctrl-C
handling and exit-code propagation is more awkward in Python than in
Go; (c) the deployability gap (`pipx install` vs. `curl | sh`) is the
biggest differentiator for end-user adoption.

**Language: Rust.** Produces equally small single binaries, with
arguably better correctness guarantees. Rejected because the build /
release / cross-compile story (`cargo dist`, `cross`, etc.) has more
moving parts than GoReleaser, and the team has even less Rust
experience than Go.

**CLI framework: stdlib `flag` only.** Smaller dependency footprint and
no third-party API to learn. Rejected because Cobra's `--` passthrough,
subcommand help, and `--version` handling cost more to reimplement than
they save, and Cobra is what users already expect from a tool of this
shape (kubectl/docker/gh).

**CLI framework: `urfave/cli`.** Lighter than Cobra, simpler API.
Rejected on familiarity grounds — Cobra is the one a new reader is most
likely to have seen before.

**WebSocket library: `gorilla/websocket`.** Battle-tested, widely used.
Rejected because the project is archived; new code should use
`coder/websocket` (the maintained spiritual successor, formerly
`nhooyr/websocket`). Migration later is cheap — the API surface we need
(`Dial`, `Read`, `Close`) is small.

**Release tooling: hand-rolled `go build` matrix in a workflow.** Avoids
a tool dependency. Rejected because we'd reinvent archive naming,
checksums, signing, and release-asset uploading — all of which
GoReleaser does correctly out of the box and which the wider Go
community already understands.

**Distribution: Homebrew / Scoop / apt / .deb / .rpm in v1.** Nice to
have, but each adds setup cost (a tap repo, signing infrastructure, or
both) and we don't yet know what fraction of users need each path. v1
ships binaries + `install.sh`; package managers follow as demand
materialises.

**Folder layout: flat (`cli/main.go` + `cli/*.go`).** Simpler to scan
for a tiny tool. Rejected because the test surface grows quickly and a
flat layout makes per-feature ownership and per-package testing harder.
The `cmd/` + `internal/` split is the convention every Go reader will
expect.

---

## Key Decisions

**Single Go module rooted at `cli/`.** Not a workspace, not a multi-module
layout. There is one binary and no library to publish; a single module is
the simplest thing that works and matches every other small CLI repo in
the Go ecosystem.

**`internal/` for everything that isn't `main`.** Compiler-enforced
boundary against accidental consumers. If we decide later to publish a
Go SDK from this code, the move into `pkg/` is a deliberate, reviewable
step.

**Cobra for command parsing.** The familiarity, `--` handling, and
auto-generated help outweigh the dependency cost. Cobra is the closest
thing Go has to a default for tools shaped like ours.

**Unknown flags are a hard error.** Cobra's underlying `pflag` parser
rejects unrecognised flags by default (`Error: unknown flag: --foo`,
non-zero exit), and we keep that default everywhere. No command sets
`FParseErrWhitelist.UnknownFlags = true`. A typo in a script should
fail loudly rather than silently changing behaviour. For `drover exec`
the `SetInterspersed(false)` setting still applies — anything after
`--` is forwarded verbatim and never flag-parsed, so `drover exec $id
-- foo --bar` passes `--bar` through cleanly while `drover exec
--bogus $id -- foo` fails with "unknown flag: --bogus".

**No config file, no profiles.** Inherited from the parent plan; restated
here because it constrains the architecture (no `viper`, no precedence
rules, no profile-switching code paths).

**JSON in / JSON out, pass-through for exec frames.** Inherited from the
parent plan. Implementation consequence: `internal/output` is a few dozen
lines, not a rendering framework, and `internal/ws` writes raw bytes
without re-marshalling.

**Exit codes are fixed and documented.** The table in "Output and exit
codes" is the contract. Scripts can rely on `3` meaning "timeout"
without parsing stderr.

**GoReleaser + GitHub Releases for distribution.** Smallest viable
publishing pipeline. Cosign-signed checksums plug into the same
verification story as the existing Docker images.

**The CLI slots into the existing versioning machinery, not its own.**
A `cli/CHANGELOG.yml`, a `cli` case in `push-tag.yml`, and a
`release-cli.yml` reusable workflow. Contributors don't learn a new
release flow.

**Pin a single Go version.** Bumping Go is a deliberate change with its
own change-file, not an incidental side effect of a CI image refresh.

---

## Open Questions

- **Where is `install.sh` served from?** Three plausible options: (a)
  GitHub Pages off this repo, (b) a dedicated `install.drover.dev`
  domain, (c) just `https://raw.githubusercontent.com/.../install.sh`
  documented as the canonical URL. (c) is zero-effort but ugly; (a)
  requires enabling Pages; (b) requires DNS. Needs a decision before
  the install instructions are written.
- **Homebrew tap in v1, or follow-up?** A tap is cheap (a second small
  repo, GoReleaser can publish to it automatically) but it's another
  thing to maintain. Worth deciding before the release process is
  cemented so we don't re-architect later.
- **Should `drover --version` include the orchestrator API version it
  was built against?** We currently have no orchestrator API version
  header. If we add one, the CLI can warn on mismatch; if we don't,
  the CLI just reports its own version. Probably worth a separate ADR
  on API versioning before answering.
- **Goldenfile policy for `testdata/`.** Do we regenerate on demand with
  `go test -update`, or treat them as hand-curated fixtures? Trivial
  either way, but pick one before the test suite grows.
- **Telemetry.** None proposed. Worth confirming the team agrees so
  nobody adds analytics later "because that's normal for CLIs".

---

## Implementation Notes

A competent Go engineer can break this into roughly the following
tickets, in dependency order:

1. **Scaffolding** — `cli/go.mod`, `cmd/drover/main.go`, empty
   `internal/` packages, Makefile, `CHANGELOG.yml`, README skeleton.
   Confirm `make build` and `make test` work on an empty tree.
2. **Config + version** — `internal/config`, `internal/version`,
   `drover --version` working with `-ldflags`-baked metadata.
3. **API client** — `internal/api` covering containers, images, execs.
   Unit tests against `httptest`.
4. **Read-only commands** — `images`, `image`, `ps`. End-to-end via
   `httptest`. First user-visible behaviour.
5. **Wait helper + lifecycle commands** — `internal/wait`, then `start`,
   `stop`, `destroy` with `--no-wait`, `--interval`, and the
   transition-timeout polling described in the parent plan.
6. **WebSocket streaming** — `internal/ws` + `exec` command. Pass-through
   frame writing, `command_id` filtering, exit-code propagation,
   SIGINT/Ctrl-C handling.
7. **Linting + CI** — `.golangci.yaml`, `cli-test.yml` workflow.
8. **Release pipeline** — `.goreleaser.yaml`, `release-cli.yml`, the
   `cli` case in `push-tag.yml`, and `versioning.md` updated.
9. **Installer** — `scripts/install.sh`, manual validation on Linux and
   macOS (Windows uses the .zip directly).
10. **E2E scenario** — extend `e2e/` to exercise the binary against a
    real orchestrator.
11. **Docs** — `cli/README.md`, `docs/cli.md`, README updates as
    enumerated in the parent plan's "Documentation Impact" section.

Steps 1–6 can land behind a "not yet released" flag (i.e. no
`CHANGELOG.yml` bump) so the binary exists but isn't published while the
release pipeline is being built out. Steps 7–9 are the ones that need
infra access (workflow secrets, install host).

---

## Risks and Mitigations

**Team unfamiliarity with Go slows code review.** Mitigation: keep the
dependency set tiny (Cobra + coder/websocket + stdlib), structure
the codebase so each file does one thing, and write generous package
comments on every `internal/*` package. The first PR should be the
scaffolding only, so reviewers learn the layout before any business
logic lands.

**GoReleaser misconfiguration leaks broken binaries.** Mitigation: the
`cli-test.yml` workflow runs `goreleaser build --snapshot` on every PR,
so configuration drift is caught before release. The first real release
is preceded by a manual `make snapshot` review by at least one team
member.

**Cosign signing breaks (keyless OIDC tends to be brittle).** Mitigation:
the release workflow's cosign step is identical to the one the Docker
images already use, so the same recovery playbook applies. If keyless
signing is unreliable, fall back to unsigned checksums (still verifiable
by sha256) and open an issue.

**`install.sh` is run by users with `curl | sh` and is therefore
security-sensitive.** Mitigation: ship the script under version control,
keep it under 200 lines, verify the checksum before extracting, and
prefer `set -euo pipefail` plus explicit `umask`. Document the
"download, inspect, then run" alternative in the install docs for
security-conscious users.

**Orchestrator API drift breaks the CLI silently.** Mitigation: the
`internal/api` types are explicit structs, not `map[string]any`, so a
removed or renamed field becomes a compile error rather than a runtime
quirk. Combined with the e2e scenario, this catches most drift in CI.

**Cross-compile produces broken binaries on a platform we don't
exercise.** Mitigation: GoReleaser's matrix is part of every PR build
(snapshot mode). For the OS/arch combos we don't run e2e on (Windows,
linux/arm64, darwin/arm64), at least one team member runs a smoke test
manually before the first release of a major version.

**Rollback story for a bad release.** Mitigation: a released binary
can't be unreleased, but the GitHub Release page can be hidden and the
`install.sh` will then skip it (it queries "latest"). A bad release
followed by an immediate patch release is the recovery path. There is
no shared state to migrate.

---

## Documentation Impact

Beyond the parent plan's documentation list:

- Add **`cli/README.md`** — quickstart for contributors (how to build
  and test locally, where each package lives, how to add a new
  subcommand).
- Add **`docs/cli.md`** — end-user installation and usage (already
  listed in the parent plan; this plan owns its initial draft).
- Update **`docs/versioning.md`** to add `cli` to the "Versioned
  projects" table and to mention the GitHub Release artefacts in the
  "Tag scheme" section.
- Update root **`README.md`** to list the CLI alongside the existing
  projects.
- New **ADRs** to write when this plan is adopted (each capturing one
  durable decision):
  - "Use Go for the Drover CLI"
  - "Cobra as the CLI framework"
  - "GoReleaser + GitHub Releases for CLI distribution"
  - "Single Go module under `cli/`, `internal/`-only packages"

Each ADR will be short — the rationale lives in this plan; the ADR
captures only the durable outcome and a pointer back here.
