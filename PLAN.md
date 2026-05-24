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
| 0 | Decisions to lock before coding | — | n/a |
| 1 | Scaffolding | 0 | no |
| 2 | Config + version | 1 | no |
| 3 | API client | 2 | no |
| 4 | Read-only commands (`images`, `image`, `ps`) | 3 | no |
| 5 | Wait helper + lifecycle (`start`/`stop`/`destroy`) | 3 | no |
| 6 | WebSocket exec streaming | 3 | no |
| 7 | Linting + test CI | 1 (grows w/ 2–6) | no |
| 8 | Release pipeline (GoReleaser, `publish-cli.yml`, tag wiring) | 6, 7 | **yes (first release)** |
| 9 | Umbrella coordination (`cli-release-assets.json`) | 8 + umbrella step 5 | yes |
| 10 | E2E scenario | 6 | no |
| 11 | Docs + ADRs | 6 (final pass after 9) | no |

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

---

## Phase 0 — Decisions to lock before coding

**Goal:** close the open questions that would force rework if answered late.
No code. Output is decisions recorded in this doc / ADR stubs.

- [ ] Confirm **module path**: `github.com/saibotsivad/drover/cli`.
- [ ] Confirm **Go version** to pin (impl proposes `go 1.23`); same value
      goes in `go.mod` and `actions/setup-go`.
- [ ] Resolve **Homebrew tap in v1?** (impl Open Questions). Default: **no**,
      `install.sh` covers users; revisit post-v1. Record the answer.
- [ ] Resolve **goldenfile policy** for `testdata/`: `go test -update`
      regeneration **vs** hand-curated. Default: **`-update` flag**. Record.
- [ ] Confirm **no telemetry** (impl Open Questions) — explicit team yes.
- [ ] Confirm **`drover --version` scope**: own version only for v1 (no
      orchestrator API-version negotiation; defer to a separate API-version
      ADR).
- [ ] Note: orchestrator already returns `transition_timeout_seconds`
      (`orchestrator/models.py:106`) — **no orchestrator-side prerequisite**.

**DoD:** every box above answered in writing; no open decision blocks Phase 1.

---

## Phase 1 — Scaffolding *(can split: tree vs. Makefile/CI-less build)*

**Goal:** an empty-but-building Go module. A reviewer learns the layout
before any logic lands. See impl §"Folder structure under `/cli`".

- [ ] `cli/go.mod` with module path + pinned Go version; `cli/go.sum`.
- [ ] `cli/cmd/drover/main.go` — wires root command, calls `Execute()`,
      nothing else.
- [ ] Empty `internal/` packages with a one-line package doc each:
      `api/`, `ws/`, `commands/`, `output/`, `wait/`, `config/`, `version/`.
- [ ] `internal/commands/root.go` — root `*cobra.Command`, registers
      (initially empty) subcommands, global flags, `--version` plumbing.
- [ ] Add `github.com/spf13/cobra` and `github.com/coder/websocket` to
      `go.mod` (no other deps — impl §"Dependencies").
- [ ] `cli/Makefile` with targets `test lint build install release snapshot`
      (impl §"Makefile"). `build`/`install` inject version via `-ldflags`.
- [ ] `cli/CHANGELOG.yml` seeded `published: "0.0.0"`, `changes: []`
      (matches existing projects; see `orchestrator/CHANGELOG.yml`).
- [ ] `cli/README.md` skeleton (quickstart + layout map; fill in Phase 11).
- [ ] `cli/.gitignore` for `bin/` and `dist/`.

**DoD:**
- `cd cli && make build` produces `cli/bin/drover`; `./bin/drover --help`
  prints root help; `./bin/drover --version` runs (value can be `(devel)`).
- `make test` passes on the empty tree (zero tests is fine).
- `internal/` packages compile. **No business logic** in this PR.

---

## Phase 2 — Config + version

**Goal:** `drover` reads/validates env and reports build metadata.
See impl §"Build & distribution" (ldflags) and parent §"Authentication".

- [ ] `internal/config/config.go`: `Load()` reads `DROVER_API_URL` /
      `DROVER_API_KEY`; returns a clear error and **exit code 2** when either
      is missing/blank. Trims/validates URL shape minimally (scheme + host).
- [ ] `internal/version/version.go`: package vars `Version`, `Commit`,
      `Date` set via `-ldflags -X`. The ldflags path must match exactly:
      ```
      -X github.com/saibotsivad/drover/cli/internal/version.Version=$(VERSION)
      ```
- [ ] Wire `--version` in `root.go` to print `Version`, `Commit`, `Date`.
- [ ] `make build` injects `git describe --tags --dirty --always` for
      non-release builds.
- [ ] Unit tests: missing var → exit 2 + stderr JSON; present vars → config
      populated; `--version` output shape.

**DoD:**
- Running with neither var set exits `2` and prints
  `{"error": ...}` to stderr.
- `make build && ./bin/drover --version` shows the git-derived version.

---

## Phase 3 — API client

**Goal:** a typed HTTP client over the orchestrator REST API. No commands
yet. See impl §"HTTP client" and §"Risks" (explicit structs, not maps).

- [ ] `internal/api/types.go`: explicit request/response structs mirroring
      `orchestrator/models.py` — `ContainerResponse` (incl. `status`,
      `error_code`, `transition_timeout_seconds`),
      `CreateContainerRequest`, `ExecResponse`, `ImageSummary`,
      `ImageDetail`. **No `map[string]any`** for known fields.
- [ ] `internal/api/client.go`: `Client` built from `config.Config`; adds
      `Authorization: Bearer` header; every method takes `context.Context`;
      single shared `*http.Client`. No retry logic here.
- [ ] `internal/api/errors.go`: decode non-2xx into `*APIError{Status, Detail}`;
      typed helpers (`IsNotFound`, etc.) as needed by commands.
- [ ] `internal/api/containers.go`: `List`, `Create`, `Get`, `Stop`,
      `Destroy` (⚠️ `DELETE /containers/{id}`).
- [ ] `internal/api/images.go`: `ListImages`, `GetImage`.
- [ ] `internal/api/execs.go`: `CreateExec` → returns `command_id`.
- [ ] Unit tests via `net/http/httptest.NewServer`: cover **every error
      branch** (4xx/5xx decoding, malformed JSON, missing fields), auth
      header presence, and the destroy-is-DELETE detail.

**DoD:**
- `go test ./internal/api/...` green, including error-path coverage.
- Client compiles against the real struct shapes (a renamed orchestrator
  field would be a compile error here — that's the point).

---

## Phase 4 — Read-only commands: `images`, `image`, `ps`

**Goal:** first user-visible behaviour. End-to-end against a fake server.
See parent §"`drover images`…/`drover ps`", impl §"Testing strategy".

- [ ] `internal/output/output.go`: `PrintJSON(any)` (compact + trailing `\n`
      to stdout) and `PrintError(err) int` (`{"error","detail"}` to stderr,
      returns exit code). Used by all commands.
- [ ] `internal/commands/images.go` → array JSON from `GET /images`.
- [ ] `internal/commands/image.go` → single object from `GET /images/{name}`.
- [ ] `internal/commands/ps.go` → array JSON from `GET /containers`.
- [ ] Register the three commands in `root.go`.
- [ ] Goldenfiles under `cli/testdata/ps/` (+ images) for table-driven tests.
- [ ] Command-level tests against `httptest`: assert exit code, stdout JSON
      (goldenfile), and stderr on the API-error path (exit 1).

**DoD:**
- `drover ps`, `drover images`, `drover image <name>` print valid JSON
  pipeable into `jq` against the fake server.
- API-error path exits `1` with `{"error","detail"}` on stderr.

---

## Phase 5 — Wait helper + lifecycle: `start`, `stop`, `destroy`

**Goal:** the polling lifecycle commands. **Depends on Phase 3** (not 4, but
reuse Phase 4's `output`). See parent §"`drover start`/`stop`/`destroy`" and
impl §"Output and exit codes".

- [ ] `internal/wait/wait.go`: `Wait(ctx, interval, deadline, fetchFn, doneFn)`.
      Interval is **sleep-between-requests** (request latency doesn't shrink
      the budget). Deadline derived from `transition_timeout_seconds`.
- [ ] `internal/commands/start.go`:
  - [ ] Flags: `--privileged` (bool), `--label` (string),
        `--env KEY=VALUE` (**`StringArrayVar`**, repeatable; KEY=VALUE
        parsed in the command for specific errors), `--timeout` (int,
        server-side cap), `--no-wait` (bool), `--interval` (int, default 1).
  - [ ] POST, read `transition_timeout_seconds`, poll `GET` to `running`.
  - [ ] `error` state → exit **4** with `{"error":"start_failed",...}`.
  - [ ] timeout → exit **3** with `{"error":"timeout","status":"initializing"}`.
  - [ ] `--no-wait` → print transitional state, exit 0.
- [ ] `internal/commands/stop.go` / `destroy.go`: `--no-wait`, `--interval`;
      poll to `stopped` / `destroyed`; timeout → exit **3** with transitional
      status in the JSON.
- [ ] Handle `transition_timeout_seconds: null` → behave as `--no-wait` +
      stderr warning.
- [ ] Register commands. Tests via `httptest` driving a status sequence
      (e.g. `initializing→running`, `initializing→error`, never-terminal→timeout).

**DoD:**
- `start` blocks to `running`, returns `{"id","status":"running"}`;
  composition `id=$(drover start img | jq -r .id)` works against fake server.
- Each failure mode returns its mandated exit code (3/4) with correct JSON.
- `--no-wait` returns immediately with transitional status.

---

## Phase 6 — WebSocket exec streaming

**Goal:** `drover exec` — pass-through frame streaming with exit-code
propagation and clean Ctrl-C. **Depends on Phase 3.** See parent
§"`drover exec`", impl §"WebSocket exec streaming" + §"Key Decisions"
(unknown-flag handling).

- [ ] `internal/ws/stream.go`: dial `WS /containers/{id}/ws` with
      `coder/websocket`; loop read → inspect `type`/`command_id` → write
      matching `output`/`status` frames **raw + `\n`** to stdout → return
      `exit_code` on matching `status:complete`. Drop `log`/other-`command_id`.
- [ ] `internal/commands/exec.go`:
  - [ ] `Use: "exec <container-id> -- <command...>"`, `Args:
        cobra.MinimumNArgs(1)`.
  - [ ] `cmd.Flags().SetInterspersed(false)` so flags stop at `--`; use
        `cmd.ArgsLenAtDash()` to find the separator. Everything after `--`
        forwarded verbatim, never flag-parsed.
  - [ ] Bare `drover exec <id>` (no `--`) → "interactive exec not yet
        supported" error.
  - [ ] POST exec → `command_id`; open WS; stream; exit with propagated code.
- [ ] Ctrl-C: wire `context.Context` to SIGINT → close socket cleanly → exit
      **130**.
- [ ] Register command. Tests: `httptest` server upgraded to WS via
      `coder/websocket`, replaying recorded frame sequences from
      `cli/testdata/exec/`; assert bytes pass through unchanged, non-matching
      frames dropped, exit code propagated, and `drover exec $id -- foo --bar`
      forwards `--bar` while `drover exec --bogus $id -- foo` fails with
      "unknown flag".

**DoD:**
- `drover exec $id -- echo hi | jq -r 'select(.stream=="stdout").data'`
  reconstructs output against the fake WS server.
- Exit code equals the status frame's `exit_code`; Ctrl-C exits 130.
- Flag-passthrough and unknown-flag behaviour verified by test.

---

## Phase 7 — Linting + test CI

**Goal:** CI guards the dev loop. Grows alongside Phases 2–6; finalise once
commands exist. See impl §"Linting" + §"CI workflows" (1).

- [ ] `cli/.golangci.yaml`: `gofmt`, `goimports`, `govet`, `staticcheck`,
      `errcheck`, `revive` (default rules).
- [ ] `make lint` runs `golangci-lint run`; passes clean on current tree.
- [ ] `.github/workflows/cli-test.yml`, `paths: cli/**`, on PR + push to
      `main`. Jobs:
  - [ ] `lint` via `golangci/golangci-lint-action`.
  - [ ] `test` via `go test ./... -race -cover`.
  - [ ] `build-matrix` via `goreleaser build --snapshot --clean` (compiles
        every target arch; **no artefacts uploaded**). *(this job depends on
        Phase 8's `.goreleaser.yaml`; until then, a `go build` cross-compile
        matrix is an acceptable placeholder — note which is in use.)*
- [ ] Uses `actions/setup-go` module cache; total budget < ~3 min.

**DoD:**
- A PR touching `cli/**` triggers `cli-test.yml`; all jobs green.
- A deliberate lint/format violation fails CI (verify once).

---

## Phase 8 — Release pipeline (GoReleaser + `publish-cli.yml` + tag wiring)

**Goal:** the CLI can be published as a per-component GitHub Release. **This
is the first phase that can produce a release.** Depends on 6 + 7. See impl
§"Build & distribution", §"CI workflows" (2)(3), §"Versioning slot".

*(can split: GoReleaser config → reusable workflow → push-tag wiring →
versioning doc)*

- [ ] `cli/.goreleaser.yaml`:
  - [ ] Builds `CGO_ENABLED=0` for linux/amd64, linux/arm64, darwin/amd64,
        darwin/arm64, windows/amd64.
  - [ ] Archives `.tar.gz` (unix) / `.zip` (windows), each bundling binary +
        README + LICENSE.
  - [ ] Single `checksums.txt`.
  - [ ] Release marked **`make_latest: false`** (umbrella owns
        `releases/latest`).
  - [ ] ldflags inject `version.Version/Commit/Date` (same path as Phase 2).
  - [ ] Cosign keyless signing of `checksums.txt` (mirror the Docker publish
        cosign step).
  - [ ] Release notes sourced from newest `cli/CHANGELOG.yml` entry.
- [ ] Naming/format of archives matches what the umbrella `install.sh`
      generator expects (coordinate with `docs/releases.md` →
      `cli-release-assets.json` platform keys: `linux-amd64`, `linux-arm64`,
      `darwin-amd64`, `darwin-arm64`, `windows-amd64`).
- [ ] `.github/workflows/publish-cli.yml` — reusable (`workflow_call`),
      input `version` (bare semver). Permissions `contents: write`,
      `id-token: write`. Runs `goreleaser release`. Emits `version` as a
      workflow output. *(the `cli-release-assets.json` step is Phase 9.)*
- [ ] Extend `.github/workflows/push-tag.yml`:
  - [ ] Add a `cli` case to the `scan` step (alongside
        orchestrator/builder/webapp) — `cli/CHANGELOG.yml` change → push
        `cli-v<version>` tag, emit `cli_version`.
  - [ ] Add `detect.outputs.cli_version`.
  - [ ] Add a `publish-cli` job: `uses: ./.github/workflows/publish-cli.yml`,
        gated `if: needs.detect.outputs.cli_version != ''`.
- [ ] Update `docs/versioning.md`: add the `cli` row to the "Versioned
      projects" table (per impl §"Versioning slot").
- [ ] `make snapshot` (`goreleaser release --snapshot --clean`) produces all
      archives locally for manual review.

**DoD:**
- `make snapshot` builds every target archive + checksums locally.
- A dry-run / test tag exercises `publish-cli.yml` end-to-end (or a
  `workflow_dispatch` against a scratch tag) producing a `cli-v<version>`
  release with `make_latest:false` and signed checksums.
- `push-tag.yml` emits `cli_version` when `cli/CHANGELOG.yml` changes.
- **Release gate:** first real release preceded by a manual `make snapshot`
  review by ≥1 teammate (impl §"Risks").

---

## Phase 9 — Umbrella coordination (`cli-release-assets.json`)

**Goal:** feed the umbrella release the CLI's download URLs + checksums.
**Depends on Phase 8 AND the umbrella plan having landed through its step 5**
(digest plumbing in `push-tag.yml`). Coordinate with the umbrella owner. See
impl §"CI workflows" (2), §"Key Decisions" (workflow-to-workflow contract),
and `docs/releases.md` §"CLI release assets".

- [ ] In `publish-cli.yml`, post-process GoReleaser's `dist/artifacts.json`
      into `cli-release-assets.json` in the **exact schema** from
      `docs/releases.md`:
      ```json
      {
        "version": "1.0.2",
        "release_url": "https://github.com/saibotsivad/drover/releases/tag/cli-v1.0.2",
        "assets": {
          "linux-amd64":  { "url": "...", "sha256": "..." },
          "linux-arm64":  { "url": "...", "sha256": "..." },
          "darwin-amd64": { "url": "...", "sha256": "..." },
          "darwin-arm64": { "url": "...", "sha256": "..." },
          "windows-amd64":{ "url": "...", "sha256": "..." }
        }
      }
      ```
- [ ] Attach `cli-release-assets.json` to the per-component release (direct
      consumers) **and** upload it as workflow artifact `cli-release-assets`.
- [ ] Confirm the umbrella builder downloads artifact `cli-release-assets`
      and that the missing-field/platform validation fails loudly (it's the
      consumer side — verify the contract end-to-end, don't reimplement).
- [ ] Add `publish-cli` to the umbrella job's `needs:` in `push-tag.yml` and
      pass `cli_version` through as the umbrella's `cli_version` input
      (mirrors the existing `*_version` / `*_digest` wiring at
      `push-tag.yml` lines ~142–164).
- [ ] Confirm "CLI unchanged in a release" path: no artifact → umbrella
      carries previous manifest's `cli` block forward (per `docs/releases.md`).

**DoD:**
- A triggered umbrella `workflow_dispatch` against a real CLI release
  produces a `manifest.yaml` with a populated `cli:` block and an
  `install.sh` carrying the CLI URLs + SHA-256s.
- Schema matches `docs/releases.md` byte-for-byte (any change here requires
  a coordinated PR on both workflows — note this in the PR description).

---

## Phase 10 — E2E scenario

**Goal:** the single CI integration point where the binary talks to a real
orchestrator. Depends on Phase 6. See impl §"CI workflows" (4) and the
existing harness under `e2e/`.

- [ ] Build the `drover` binary in an e2e setup step.
- [ ] New scenario script under `e2e/tests/` (mirror the existing
      `0N-*.sh` numbered style; reuse `e2e/lib/*.sh` helpers): drive
      `drover ps → start → exec → stop` against the live orchestrator from
      `e2e/docker-compose.e2e.yml`.
- [ ] Assert: `start` reaches `running`, `exec` streams expected stdout,
      `stop` reaches `stopped`, exit codes correct.
- [ ] Wire into `.github/workflows/e2e.yml`.

**DoD:**
- The CLI e2e scenario passes in CI against a real orchestrator.

---

## Phase 11 — Docs + ADRs

**Goal:** make the CLI discoverable and the decisions durable. Final pass
after Phase 9. See impl §"Documentation Impact".

- [ ] `cli/README.md` — contributor quickstart: build/test locally, package
      layout map, how to add a new subcommand.
- [ ] `docs/cli.md` — **end-user usage** (commands, flags, JSON output, exec
      streaming, exit codes). **Installation lives in `docs/releases.md`** —
      link to it, don't duplicate.
- [ ] `docs/versioning.md` "Versioned projects" table updated (if not already
      done in Phase 8).
- [ ] Root `README.md` — list the CLI alongside orchestrator/builder/webapp.
- [ ] Update `docs/exec-commands.md` with a note on how the CLI's streaming
      maps to the exec flow (parent plan Documentation Impact).
- [ ] ADRs under `docs/decisions/` (short — rationale lives in the planning
      docs; ADR captures the durable outcome + a pointer back):
  - [ ] "Use Go for the Drover CLI"
  - [ ] "Cobra as the CLI framework"
  - [ ] "Single Go module under `cli/`, `internal/`-only packages"
  - [ ] *(No ADR needed for GoReleaser/per-component release — it's a
        mechanical consequence of the existing umbrella-release ADR
        `docs/decisions/2026-05-23-github-release-as-manifest.md`.)*

**DoD:**
- A new contributor can go from clone → `make test`/`make build` using only
  `cli/README.md`.
- An end user can find usage in `docs/cli.md` and install via the
  `docs/releases.md` flow.

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
