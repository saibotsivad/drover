# Drover CLI — Build Plan

> **Working tracker for building the `drover` Go CLI.** This is the
> phase-by-phase execution plan the team checks off as work lands. It does
> **not** restate the design rationale — that lives in the two source
> documents below. Read those first; come here to know *what to build next*
> and *how to tell it's done*.
>
> - **User-facing contract** (commands, flags, JSON, exit behaviour):
>   [`docs/planning/drover-cli.md`](./docs/planning/drover-cli.md)
> - **Engineering design** (layout, deps, release machinery, rationale):
>   [`docs/planning/drover-cli-go-implementation.md`](./docs/planning/drover-cli-go-implementation.md)
> - **Release/versioning context:**
>   [`docs/versioning.md`](./docs/versioning.md),
>   [`docs/releases.md`](./docs/releases.md)
>
> When a phase note says "see impl §X", it points at a section heading in
> `drover-cli-go-implementation.md`.

---

## How to use this document

- Phases are ordered by dependency. Don't start a phase until its
  **Depends on** phases are checked off, unless noted as parallelisable.
- Each phase has a **Definition of Done (DoD)** — the objective gate that
  must be true before the phase is considered complete. Check the phase
  box only when every DoD item is true.
- One PR per phase is the default (keeps reviews small for a team learning
  Go). Phases explicitly marked *(can split)* may land in multiple PRs.
- **Phases 1–6 land with no `CHANGELOG.yml` bump** — the binary exists in
  the tree but is not published. Versioning/release wiring (Phases 8–9) is
  what makes a release possible. See impl §"Implementation Notes" last
  paragraph.

### Status legend

`[ ]` not started  ·  `[~]` in progress  ·  `[x]` done

### Phase overview

| # | Phase | Depends on | Lands a release? |
|---|---|---|---|
| 0 | Decisions to lock before coding ✅ | — | n/a |
| 1 | Scaffolding ✅ | 0 | no |
| 2 | Config + version ✅ | 1 | no |
| 3 | API client ✅ | 2 | no |
| 4 | Read-only commands (`images`, `image`, `ps`) ✅ | 3 | no |
| 5 | Wait helper + lifecycle (`start`/`stop`/`destroy`) ✅ | 3 | no |
| 6 | WebSocket exec streaming ✅ | 3 | no |
| 7 | Linting + test CI ✅ | 1 (grows w/ 2–6) | no |
| 8 | Release pipeline (GoReleaser, `publish-cli.yml`, tag wiring) ✅ | 6, 7 | **yes (first release)** |
| 9 | Umbrella coordination (`cli-release-assets.json`) ◑ producer done | 8 + umbrella step 5 | yes |
| 10 | E2E scenario ✅ | 6 | no |
| 11 | Docs + ADRs ✅ | 6 (final pass after 9) | no |

---

## Reference contracts (pin these — every phase depends on them)

These are extracted here so an engineer building one command doesn't have
to reconstruct the whole picture. They are **the contract**; if reality
disagrees, fix the code, not the table (or raise it in review).

### Command → API mapping

| Command | HTTP | Notes |
|---|---|---|
| `drover images` | `GET /images` | JSON array out |
| `drover image <name>` | `GET /images/{name}` | single JSON object |
| `drover ps` | `GET /containers` | JSON array out |
| `drover start <image>` | `POST /containers` (201) | body carries flags; response has `transition_timeout_seconds`; then poll |
| `drover stop <id>` | `POST /containers/{id}/stop` | response has `transition_timeout_seconds`; then poll |
| `drover destroy <id>` | **`DELETE /containers/{id}`** | ⚠️ destroy is a `DELETE`, not a POST; then poll |
| `drover exec <id> -- <cmd…>` | `POST /containers/{id}/execs` (201) → `WS /containers/{id}/ws` | POST returns `command_id`; stream filtered by it |

Auth: every request sends `Authorization: Bearer <DROVER_API_KEY>` (matches
`orchestrator/auth.py`). Base URL from `DROVER_API_URL`.

### Container status values (from `orchestrator/models.py`)

`initializing · running · stopping · stopped · resuming · destroying ·
destroyed · error`

Terminal targets per command: `start → running`, `stop → stopped`,
`destroy → destroyed`. `start` additionally treats `error` as failure.

### WebSocket frame shapes (from `orchestrator/routers/websockets.py`)

```jsonc
{"type": "output", "command_id": "...", "stream": "stdout|stderr", "data": "..."}
{"type": "status", "command_id": "...", "status": "complete", "exit_code": 0}
{"type": "log",    "stream": "stdout|stderr", "data": "..."}   // dropped by exec
{"type": "error",  "message": "..."}
```

`exec` writes matching `output`/`status` frames through **verbatim** (one
JSON object per line, no re-marshalling), drops everything else, and exits
on the matching `status: complete` frame propagating `exit_code`.

### Exit codes (the contract — impl §"Output and exit codes")

| Situation | Code |
|---|---|
| Success | 0 |
| `drover exec` — propagated from status frame `exit_code` | 0–255 |
| Generic API error (4xx/5xx with `detail`) | 1 |
| Missing/invalid `DROVER_API_URL` / `DROVER_API_KEY` | 2 |
| Polling timeout (`start`/`stop`/`destroy`) | 3 |
| `drover start` ended in `error` state | 4 |
| SIGINT (Ctrl-C) | 130 |

### Decided-elsewhere facts to honour

- **No config file.** Env vars only. No viper, no profiles.
- **All control-plane output is JSON** (objects, not bare scalars), errors
  as `{"error": "...", "detail": "..."}` on stderr.
- **Exact container-ID matching**, no prefix matching.
- **`transition_timeout_seconds: null`** from an endpoint → treat as
  `--no-wait` and warn on stderr (impl/parent: no defensible default).
- **Bare `drover exec <id>`** (no `--`) → error "interactive exec not yet
  supported".
- **No telemetry, ever.** No analytics, no phone-home, no usage counters —
  in any package, including future additions (Phase 0 team decision).
- **Goldenfiles regenerate via `go test -update`** (Phase 0 decision): tests
  read fixtures by default; the flag rewrites them for review in the diff.

---

## Phase 0 — Decisions to lock before coding — ✅ DONE

**Goal:** close the open questions that would force rework if answered late.
No code. Output is decisions recorded in this doc / ADR stubs.

- [x] **Module path:** `github.com/saibotsivad/drover/cli` (module rooted at
      `cli/` of the repo). *(confirmed)*
- [x] **Go version:** pin `go 1.23` — same value in `go.mod` and
      `actions/setup-go`. *(confirmed)*
- [x] **Homebrew tap in v1?** **No.** `install.sh` covers users; revisit
      post-v1. Not implemented in this plan. *(confirmed)*
- [x] **Goldenfile policy for `testdata/`:** **regenerate with `go test
      -update`** (idiomatic Go; keeps fixtures in sync with code cheaply,
      regenerated diff is reviewed in the PR). Tests read fixtures by default;
      a `-update` flag rewrites them. *(engineering decision)*
- [x] **Telemetry:** **none, ever.** No analytics, no phone-home. *(team
      decision — absolute)*
- [x] **`drover --version` scope:** reports the **CLI's own version only** for
      v1. No orchestrator API-version negotiation; defer that to a separate
      API-version ADR if/when needed. *(confirmed)*
- [x] **Orchestrator prerequisite:** none — orchestrator already returns
      `transition_timeout_seconds` (`orchestrator/models.py:106`).

**DoD:** ✅ every decision above answered in writing; no open decision blocks
Phase 1.

---

## Phase 1 — Scaffolding — ✅ DONE *(can split: tree vs. Makefile/CI-less build)*

**Goal:** an empty-but-building Go module. A reviewer learns the layout
before any logic lands. See impl §"Folder structure under `/cli`".

- [x] `cli/go.mod` with module path + pinned Go version (`go 1.23`); `cli/go.sum`.
- [x] `cli/cmd/drover/main.go` — wires root command, calls `Execute()`,
      nothing else.
- [x] Empty `internal/` packages with a one-line package doc each
      (`doc.go`): `api/`, `ws/`, `output/`, `wait/`, `config/`. (`commands/`
      and `version/` carry their docs on `root.go` / `version.go`.)
- [x] `internal/commands/root.go` — root `*cobra.Command`, `--version`
      plumbing, `SilenceUsage/Errors`; subcommand registration point marked
      (commands land in later phases).
- [x] Add `github.com/spf13/cobra` to `go.mod`. **`github.com/coder/websocket`
      deferred to Phase 6** — `go mod tidy` prunes any dependency nothing
      imports, so it is added when `ws/stream.go` first uses it rather than
      carried as a phantom require (impl §"Dependencies" still holds: final
      dep set is cobra + coder/websocket + stdlib).
- [x] `cli/Makefile` with targets `test lint build install release snapshot`
      (impl §"Makefile"). `build`/`install` inject version via `-ldflags`.
- [x] `cli/CHANGELOG.yml` seeded `published: "0.0.0"`, `changes: []`
      (matches existing projects; see `orchestrator/CHANGELOG.yml`).
- [x] `cli/README.md` skeleton (quickstart + layout map; fill in Phase 11).
- [x] `cli/.gitignore` for `bin/` and `dist/`.

**DoD:** ✅
- `make build` produces `cli/bin/drover`; `./bin/drover --version` prints the
  git-derived version; `./bin/drover --help` exits 0. *(Note: Cobra omits the
  Usage/Flags block until the root has subcommands or a Run — full help
  renders automatically once Phase 4 registers commands. Correct Cobra
  behaviour, not a gap.)*
- `make test` passes on the empty tree; `gofmt -l .` and `go vet ./...` clean.
- `internal/` packages compile. No business logic.

---

## Phase 2 — Config + version — ✅ DONE

**Goal:** `drover` reads/validates env and reports build metadata.
See impl §"Build & distribution" (ldflags) and parent §"Authentication".

- [x] `internal/config/config.go`: `Load()` reads `DROVER_API_URL` /
      `DROVER_API_KEY`; returns an `*output.Failure` with **exit code 2** when
      either is missing/blank, and `invalid_configuration` (also exit 2) when
      the URL has no http(s) scheme or host. Trailing slash trimmed.
- [x] `internal/version/version.go`: package vars `Version`, `Commit`,
      `Date` set via `-ldflags -X`. The ldflags path matches exactly:
      ```
      -X github.com/saibotsivad/drover/cli/internal/version.Version=$(VERSION)
      ```
- [x] `--version` wired in `root.go` (`SetVersionTemplate`) → prints
      `Version`, `Commit`, `Date`.
- [x] `make build` injects `git describe --tags --dirty --always`.
- [x] **`internal/output/output.go` landed here (pulled forward from Phase 4):**
      `PrintJSON(w, v)`, `PrintError(w, err) int`, and the `Failure` error type
      carrying the exit code + JSON body. Established now because exit-code
      handling is foundational; `Execute` renders all errors through it.
- [x] Unit tests: config table (missing/blank/invalid → exit 2; valid →
      populated), output (PrintJSON shape, PrintError for Failure/plain/wrapped),
      version string shape.

**DoD:** ✅
- With neither var set, a command that calls `config.Load` exits `2` with a
  JSON error on stderr.
- `make build && ./bin/drover --version` shows the git-derived version.
- `go test ./...`, `gofmt -l .`, `go vet ./...` clean.

---

## Phase 3 — API client — ✅ DONE

**Goal:** a typed HTTP client over the orchestrator REST API. No commands
yet. See impl §"HTTP client" and §"Risks" (explicit structs, not maps).

- [x] `internal/api/types.go`: `Status` enum + constants;
      `CreateContainerRequest`; `Container` (typed view: id, image,
      privileged, status, label, timeout_seconds, error_code,
      transition_timeout_seconds); `ContainerResult` (typed view **+ raw
      bytes**); `ExecRequest`/`ExecResponse`. **Design choice:** display
      commands (`images`/`ps`) get the orchestrator JSON **verbatim** (raw
      passthrough preserves the "shape can grow" promise); container lifecycle
      methods decode the typed view *and* keep `Raw`. **No image structs** —
      nothing reads image fields, so typed image structs would be dead code
      giving no drift protection.
- [x] `internal/api/client.go`: `New(baseURL, apiKey)` (takes strings, not
      `config.Config`, to avoid api→config coupling); `Authorization: Bearer`
      + `Accept` headers; every method takes `context.Context`; shared
      `*http.Client` (60s per-request timeout). No retry logic.
- [x] `internal/api/errors.go`: `*APIError{Status, Kind, Detail}` (exit 1),
      handles FastAPI string **and** list `detail`, non-JSON bodies, and
      transport failures (`request_failed`); `IsNotFound` helper.
- [x] `internal/api/containers.go`: `ListContainers` (raw), `GetContainer`,
      `CreateContainer`, `StopContainer`, `DestroyContainer`
      (⚠️ `DELETE /containers/{id}`). Path segments `url.PathEscape`d —
      correct for the orchestrator's single-segment `{name}` routes (slashes
      arrive as `%2F`).
- [x] `internal/api/images.go`: `ListImages`, `GetImage` (raw passthrough).
- [x] `internal/api/execs.go`: `CreateExec` → returns `command_id`.
- [x] Unit tests via `httptest`: auth/Accept headers, every error branch
      (404 string detail, 422 list detail, non-JSON body, transport refusal),
      raw passthrough preserves unknown fields, **destroy-is-DELETE**,
      method/path assertions, exec round-trip.

**DoD:** ✅
- `go test ./internal/api/... -race` green, including error-path coverage.
- Client compiles against the real struct shapes; the typed `Container` view
  turns a renamed `status`/`transition_timeout_seconds` into a zero value the
  e2e/lifecycle tests would catch.

---

## Phase 4 — Read-only commands: `images`, `image`, `ps` — ✅ DONE

**Goal:** first user-visible behaviour. End-to-end against a fake server.
See parent §"`drover images`…/`drover ps`", impl §"Testing strategy".

- [x] `internal/output/output.go` — **done in Phase 2.** `PrintJSON(w, v)`
      (compact + trailing `\n`) and `PrintError(w, err) int` (JSON to stderr,
      returns exit code). Writers are passed in (from `cmd.OutOrStdout()` /
      `ErrOrStderr()`) so command output is capturable in tests.
- [x] `internal/commands/images.go` → array JSON from `GET /images`.
- [x] `internal/commands/image.go` → single object from `GET /images/{name}`.
- [x] `internal/commands/ps.go` → array JSON from `GET /containers`.
- [x] Registered all three in `root.go`; added `clientFromEnv` helper and a
      testable `execute(root)` split out of `Execute`.
- [x] Goldenfiles + `-update` flag wiring. **Relocated to package-local
      `internal/commands/testdata/*.golden`** (idiomatic Go — `go test`
      auto-ignores `testdata/` and tests resolve `./testdata`) rather than a
      top-level `cli/testdata/`. The `cli/testdata/{ps,exec}` layout in the
      impl doc is superseded by per-package `testdata/`.
- [x] Command-level tests against `httptest`: exit code, stdout goldenfile,
      API-error path (exit 1, empty stdout, `api_error` on stderr), plus
      missing-config (exit 2) and unknown-flag (exit 1) cases.

**DoD:** ✅
- `drover ps`, `drover images`, `drover image <name>` print valid passthrough
  JSON against the fake server; `--help` now lists the commands.
- API-error path exits `1` with the JSON error on stderr; `go test ./... -race`,
  `gofmt`, `go vet` clean.

---

## Phase 5 — Wait helper + lifecycle: `start`, `stop`, `destroy` — ✅ DONE

**Goal:** the polling lifecycle commands. **Depends on Phase 3** (not 4, but
reuse Phase 4's `output`). See parent §"`drover start`/`stop`/`destroy`" and
impl §"Output and exit codes".

- [x] `internal/wait/wait.go`: generic `Wait[T](ctx, interval, deadline,
      fetch, done)`. Interval is **sleep-between-requests**; clamps to 1s if
      ≤0. Returns the last fetched value with `ErrTimeout` so callers can
      report the transitional status. Unit-tested directly (done-first,
      done-after-N, timeout, fetch error, ctx-cancel) with ms intervals.
- [x] `internal/commands/start.go`:
  - [x] Flags: `--privileged`, `--label`, `--env KEY=VALUE`
        (`StringArrayVar`, repeatable; parsed in `parseEnv` for specific
        errors), `--timeout` (0 = server default via omitempty), `--no-wait`,
        `--interval` (default 1).
  - [x] POST, read `transition_timeout_seconds`, poll `GET` to `running`.
  - [x] `error` state → exit **4** `{"error":"start_failed",...}`.
  - [x] timeout → exit **3** `{"error":"timeout","status":"initializing"}`.
  - [x] `--no-wait` → print transitional state, exit 0 (verified GET not polled).
- [x] `internal/commands/stop.go` / `destroy.go`: `--no-wait`, `--interval`;
      poll to `stopped` / `destroyed`; share `runLifecycle`. Destroy uses
      `DELETE`.
- [x] `transition_timeout_seconds: null` → JSON warning to stderr + print
      transitional + exit 0 (behaves as `--no-wait`).
- [x] Registered all three. `httptest` tests drive status sequences
      (`initializing→running`, `→error`, zero-deadline→timeout); tests avoid
      real sleeps via terminal-on-first-poll / zero deadline.

**DoD:** ✅
- `start` blocks to `running` and prints the full container JSON (incl. `id`),
  so `id=$(drover start img | jq -r .id)` works against the fake server.
- Failure modes return their mandated exit codes (3 timeout / 4 start_failed)
  with correct JSON; `--no-wait` returns the transitional status immediately.
- `go test ./... -race`, `gofmt`, `go vet` clean.
- *(Note: `context.Canceled` is already mapped to exit 130 in `runLifecycle`;
  the signal context that triggers it is wired in Phase 6.)*

---

## Phase 6 — WebSocket exec streaming — ✅ DONE

**Goal:** `drover exec` — pass-through frame streaming with exit-code
propagation and clean Ctrl-C. **Depends on Phase 3.** See parent
§"`drover exec`", impl §"WebSocket exec streaming" + §"Key Decisions"
(unknown-flag handling).

- [x] Added `github.com/coder/websocket` to `go.mod` (now a direct require).
- [x] `internal/ws/stream.go`: `URL()` converts `http(s)`→`ws(s)` and appends
      `/containers/{id}/ws`; `Stream()` dials with bearer header, sets a 32 MiB
      read limit, loops read → inspect `type`/`command_id` → write matching
      `output`/`status` frames **raw + `\n`** → return `exit_code` on matching
      `status:complete`. Drops `log`/other-`command_id` frames.
- [x] `internal/commands/exec.go`:
  - [x] `Use: "exec <container-id> -- <command...>"`, `Args: MinimumNArgs(1)`.
  - [x] **Correction to the impl doc:** uses cobra's **default** interspersed
        parsing + `cmd.ArgsLenAtDash()`. `SetInterspersed(false)` is
        deliberately **not** used — empirically it makes `ArgsLenAtDash()`
        always `-1`. Default parsing gives dash=1 for `id -- cmd`, `-1` for
        bare, and still rejects a flag before the id. Everything after `--`
        is forwarded verbatim.
  - [x] Bare `drover exec <id>` (no `--`) → `interactive_exec_unsupported`
        (exit 1).
  - [x] POST exec → `command_id`; build WS URL; stream; propagate exit code
        via `output.SilentExit` (non-zero exit, **no** error envelope since
        output already streamed).
- [x] Ctrl-C: `Execute` runs under `signal.NotifyContext(…, os.Interrupt)`;
      a cancelled context maps to exit **130** in `exec` and `runLifecycle`.
- [x] Registered command. Tests: `httptest` upgraded to WS via
      `coder/websocket` (frames inline, not files): pass-through verbatim,
      non-matching/`log` frames dropped, exit-code propagation (5 and 0),
      `exec $id -- ls --bar -la` posts `"ls --bar -la"`, bare exec → exit 1,
      `exec --bogus $id -- ls` → unknown flag (exit 1), stream-closed-before-
      complete → error. `ws` package unit-tests `URL`, passthrough, and
      cancellation.

**DoD:** ✅
- `drover exec $id -- echo hi` streams newline-delimited frames to stdout
  (jq-reconstructable) against the fake WS server.
- Exit code equals the status frame's `exit_code` (SilentExit, clean stderr);
  cancellation path exits 130.
- Flag-passthrough and unknown-flag behaviour verified by test.
- `go test ./... -race`, `gofmt`, `go vet` clean.

---

## Phase 7 — Linting + test CI — ✅ DONE (lint verified locally; CI authored)

**Goal:** CI guards the dev loop. Grows alongside Phases 2–6; finalise once
commands exist. See impl §"Linting" + §"CI workflows" (1).

- [x] `cli/.golangci.yaml` — **golangci-lint v2 format** (`version: "2"`).
      Linters: `errcheck`, `govet`, `staticcheck`, `revive`, `unused`;
      formatters: `gofmt`, `goimports`. Test-only exclusions relax `errcheck`
      and revive `unused-parameter` for HTTP/WS handler boilerplate.
- [x] `make lint` runs `golangci-lint run`; **verified clean (0 issues)**
      locally with golangci-lint v2.5.0. Fixed real findings: unchecked
      `Close`/`Fprintf` errors in production code; renamed `api.APIError` →
      `api.Error` (stutter) and `parseAPIError` → `parseError`; added const-
      block comment.
- [x] `.github/workflows/cli-test.yml`, `paths: cli/**`, on PR + push to
      `main`. Jobs: `lint` (golangci-lint-action, pinned v2.5.0), `test`
      (`go test ./... -race -cover`), `build-matrix`
      (`goreleaser build --snapshot --clean` — cross-compiles all targets and
      validates `.goreleaser.yaml`; no artefacts).
- [x] Uses `actions/setup-go` module cache (`cache-dependency-path: cli/go.sum`).

**DoD:**
- [x] Lint passes clean locally; a deliberate violation fails it (errcheck
      caught the real ones above).
- [ ] **Not yet observed in CI** (no PR opened; this branch isn't `main`, so
      the workflow hasn't been triggered). `build-matrix` depends on the
      Phase 8 `.goreleaser.yaml`, which lands on the same branch — verify both
      green on the first PR. golangci-lint-action ↔ linter v2 version pin may
      need a nudge in CI.

---

## Phase 8 — Release pipeline — ✅ DONE (authored; needs first-run validation)

**Goal:** the CLI can be published as a per-component GitHub Release. **This
is the first phase that can produce a release.** Depends on 6 + 7. See impl
§"Build & distribution", §"CI workflows" (2)(3), §"Versioning slot".

- [x] `cli/.goreleaser.yaml` (GoReleaser **v2** schema):
  - [x] `CGO_ENABLED=0` builds for linux/amd64, linux/arm64, darwin/amd64,
        darwin/arm64, windows/amd64 (windows/arm64 ignored).
  - [x] Archives `.tar.gz` (unix) / `.zip` (windows) via `format_overrides`;
        default `files` bundles README + CHANGELOG (no LICENSE file exists).
  - [x] `checksums.txt`.
  - [x] `release.make_latest: false` (umbrella owns `releases/latest`).
  - [x] ldflags inject `version.Version/Commit/Date` (same path as Phase 2).
  - [x] Cosign keyless `sign-blob` over the checksums file.
  - [x] `monorepo.tag_prefix: cli-` strips the `cli-` from the
        `cli-v<version>` tag so the version parses as semver.
  - [ ] **Deferred:** release notes from `cli/CHANGELOG.yml` — using
        GoReleaser's default git-based notes for now (umbrella owns the
        human changelog). Low priority.
- [x] Archive `name_template: drover-{{.Os}}-{{.Arch}}` →
      `drover-linux-amd64.tar.gz` etc., matching the `cli-release-assets.json`
      platform keys and umbrella `install.sh` (docs/releases.md).
- [x] `.github/workflows/publish-cli.yml` — reusable (`workflow_call`), input
      `version`, perms `contents: write` + `id-token: write`, runs
      `goreleaser release` with `workdir: cli` and
      `GORELEASER_CURRENT_TAG: cli-v<version>`; emits `version` output.
      (`cli-release-assets.json` step added in Phase 9.)
- [x] Extended `.github/workflows/push-tag.yml`: `cli` added to the
      `workflow_dispatch` choices and both `scan` branches; `cli_version`
      output; `publish-cli` job gated on `cli_version != ''`.
- [x] `docs/versioning.md` "Versioned projects" table gains the `cli` row.

**DoD:** ⚠️ authored, **not yet executed** (goreleaser/cosign not available in
this environment):
- All workflow + goreleaser YAML parses (validated with a YAML loader).
- [ ] `make snapshot` builds every target archive locally — **run on a
      machine with goreleaser before first release.**
- [ ] A `workflow_dispatch` against a scratch `cli-v…` tag exercises
      `publish-cli.yml` (cosign keyless, `make_latest:false`).
- [ ] **Validate `goreleaser check`** — especially the v2 `formats` keys and
      that `monorepo.tag_prefix` parses `cli-v<version>` (this is the highest-
      risk untested assumption).
- **Release gate:** first real release preceded by a manual `make snapshot`
  review by ≥1 teammate (impl §"Risks").

---

## Phase 9 — Umbrella coordination (`cli-release-assets.json`) — ◑ PRODUCER DONE

**Goal:** feed the umbrella release the CLI's download URLs + checksums.
**Depends on Phase 8 AND the umbrella plan having landed through its step 5.**
See impl §"CI workflows" (2), §"Key Decisions" (workflow-to-workflow
contract), and `docs/releases.md` §"CLI release assets".

**Status:** the **producer** is fully implemented; the **consumer** is
intentionally left to the umbrella plan — `umbrella-release.yml` already
declares a `cli_version` input documented as *"Reserved … carried forward
from previous manifest until then,"* so the manifest `cli:` block + `install.sh`
baking is that plan's remaining work, not reimplemented here.

- [x] In `publish-cli.yml`, post-process GoReleaser's `dist/artifacts.json`
      (+ `dist/checksums.txt`) into `cli-release-assets.json` in the exact
      `docs/releases.md` schema (`version`, `release_url`, per-platform
      `{url, sha256}`). Fails loudly if any archive lacks a checksum.
- [x] Attach `cli-release-assets.json` to the per-component release
      (`gh release upload --clobber`) **and** upload it as workflow artifact
      `cli-release-assets` (`if-no-files-found: error`).
- [ ] **Consumer (umbrella plan, not this work):** umbrella `build` job must
      `download-artifact` `cli-release-assets`, populate the manifest `cli:`
      block, and bake URLs+SHA-256s into `install.sh`, with loud
      missing-field/platform validation. Today it carries the previous
      manifest's `cli` block forward (per the input's doc comment).
- [x] Added `publish-cli` to the umbrella job's `needs:` + skip-gating in
      `push-tag.yml` and pass `cli_version` to umbrella (input already exists).
      Reusable workflows share a run, so the `cli-release-assets` artifact is
      downloadable by the umbrella job in the same run.
- [x] "CLI unchanged" path: `publish-cli` is skipped, no artifact → umbrella
      carries the previous manifest's `cli` block forward.

**DoD:** ◑ producer authored & YAML-valid; **end-to-end pending** the umbrella
consumer + a real release:
- [ ] A triggered umbrella run against a real CLI release produces a
      `manifest.yaml` `cli:` block + `install.sh` with the URLs/SHA-256s
      (blocked on the consumer above).
- [x] Schema matches `docs/releases.md` (any change needs a coordinated PR on
      both producer and the umbrella consumer — call this out in the PR).

---

## Phase 10 — E2E scenario — ✅ DONE (authored; not run here)

**Goal:** the single CI integration point where the binary talks to a real
orchestrator. Depends on Phase 6. See impl §"CI workflows" (4) and the
existing harness under `e2e/`.

- [x] `e2e/tests/07-cli.sh` (next number after 06; sources `lib/common.sh`,
      uses the real `step_begin/step_end/assert_*` helpers — all verified to
      exist). The test owns building the binary
      (`cd cli && go build -o bin/drover ./cmd/drover`), matching the
      repo convention.
- [x] Drives `drover ps → start builder --privileged → exec … -- echo hello
      → stop → destroy` against the live stack. Uses the **privileged** path
      (like test 06) so it needs no gVisor. Env: `DROVER_API_URL=$ORCHESTRATOR_URL`,
      `DROVER_API_KEY` from `.env.test`.
- [x] Asserts: `ps` emits a JSON array; `start` returns an `.id` and reaches
      `running`; `exec` streams `hello` on stdout and propagates exit 0
      (both the process code and the status-frame `exit_code`); `stop`→
      `stopped`; `destroy`→`destroyed`. `EXIT` trap best-effort destroys.
- [x] `.github/workflows/e2e.yml` gains `actions/setup-go@v5` (1.23, cache on
      `cli/go.sum`); `run.sh` auto-discovers `tests/*.sh` so no list edit; `jq`
      already installed by the workflow.

**DoD:** ⚠️ authored, **not run here** (no Docker/gVisor/orchestrator in this
env). `bash -n` clean; helper names + env vars verified against `e2e/lib` and
`.env.test`.
- [ ] Run the scenario in CI (`e2e.yml` is `workflow_dispatch`) against a real
      orchestrator and confirm green.

---

## Phase 11 — Docs + ADRs — ✅ DONE

**Goal:** make the CLI discoverable and the decisions durable. Final pass
after Phase 9. See impl §"Documentation Impact".

- [x] `cli/README.md` — finalized contributor quickstart: build/test/lint via
      the Makefile, accurate `internal/` layout, add-a-subcommand steps tied
      to real symbols (`newRootCmd`, `clientFromEnv`, `runLifecycle`,
      `output.Failure`).
- [x] `docs/cli.md` — end-user usage: config, JSON output contract, all 7
      commands with HTTP mappings (incl. `destroy = DELETE`), flag tables,
      lifecycle polling semantics, exec streaming + `jq` reconstruction +
      interactive error, and the full exit-code table. Links to
      `docs/releases.md#installation` rather than duplicating install.
- [x] `docs/versioning.md` "Versioned projects" table — done in Phase 8.
- [x] Root `README.md` — added a "Command-Line Client" section linking
      `docs/cli.md`, the release install flow, and `cli/README.md`.
- [x] `docs/exec-commands.md` — appended a note on how the CLI consumes the
      stream (POST execs → WS, filter by `command_id`, verbatim frames, exit
      with the command's code).
- [x] ADRs under `docs/decisions/` (match the repo's `YYYY-MM-DD-kebab.md` +
      `# ADR:` / `**Date:**` / `**Status:** Accepted` format; each points back
      to the planning doc):
  - [x] `2026-05-24-go-for-the-cli.md`
  - [x] `2026-05-24-cobra-cli-framework.md`
  - [x] `2026-05-24-single-go-module-internal-packages.md`
  - [x] *(No GoReleaser ADR — mechanical consequence of the umbrella-release
        ADR, as noted.)*

**DoD:** ✅
- Contributor can go clone → `make build`/`make test` from `cli/README.md`.
- End user finds usage in `docs/cli.md` and installs via `docs/releases.md`.
- Facts in the docs were cross-checked against the source (exit codes,
  `DELETE` destroy, interactive error string, links all verified).

---

## Cross-cutting risks to watch (impl §"Risks and Mitigations")

- **Team unfamiliarity with Go** → tiny dep set, one-thing-per-file,
  package comments, scaffolding PR first.
- **GoReleaser misconfig** → snapshot build runs on every PR (Phase 7);
  manual snapshot review before first release.
- **Cosign keyless brittleness** → identical to the Docker cosign step;
  fall back to unsigned-but-sha256-verifiable checksums + open an issue.
- **`cli-release-assets.json` schema drift** → schema in `docs/releases.md`
  is the source of truth; producer (Phase 9) + consumer (umbrella) change
  together in one PR.
- **Orchestrator API drift** → explicit structs (Phase 3) turn renamed
  fields into compile errors; e2e (Phase 10) catches the rest.
- **Bad release rollback** → can't unrelease a binary; hide the
  per-component release, ship a fixed CLI version, publish a new umbrella
  release pinning it.
