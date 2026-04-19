# Zero-Config Bootstrap Detection

**Status:** Proposed. **Depends on:** [2026-04-17-better-startup-flow.md](2026-04-17-better-startup-flow.md).

This RFC adds zero-configuration dependency installation to the auto-init flow defined in the Better Startup Flow RFC. When a cloned repository contains no `drover.yaml`, the executor inspects the working tree for well-known manifest files and runs a conventional install command for the first match.

This is strictly a follow-up: the parent RFC must ship first, because detection hooks into the same `on_init` phase and reuses the `__init__` synthetic command ID, the init timeout, and the `init_failed` reporting path.

---

## Motivation

The parent RFC already delivers the common case: clone a repo, run `drover.yaml`'s `setup` commands, report `ready`. But most small repos do not carry a `drover.yaml` — they carry a `requirements.txt`, a `package.json`, or a `Cargo.toml`. Requiring every operator to author a `drover.yaml` for each repo they want to run raises the floor of effort for exactly the drive-by workloads Drover is meant to make cheap.

A small, strict detector covers the 80% case without asking anyone to write a manifest.

---

## Scope

In scope: a deterministic detection pass that runs **only** when `drover.yaml` is absent, produces a synthetic setup plan, and feeds it into the same `run_setup` path the parent RFC defines.

Out of scope: language version selection, toolchain installation (the image must already contain `pip`, `npm`, `cargo`, etc.), multi-language projects, monorepos, lockfile-aware resolution beyond the single `package.json`/`package-lock.json` case.

---

## Detection Order

First match wins. Detection stops at the first file found; it does not try to combine multiple detectors.

1. `pyproject.toml` → `pip install .`
   If a `[project.optional-dependencies].dev` table exists, use `pip install .[dev]`.
2. `requirements.txt` → `pip install -r requirements.txt`
3. `package.json` with `package-lock.json` adjacent → `npm ci`
4. `package.json` without a lockfile → `npm install`
5. `Cargo.toml` → `cargo fetch`
6. `go.mod` → `go mod download`
7. `Gemfile` → `bundle install`

No match: executor logs `detected=none` and goes straight to `ready`. This is deliberate — some workloads need only the source tree, and silently doing nothing is better than guessing wrong.

The order is Python-first because the initial base image is `drover/python-runner`. When additional base images land (Node, Rust, Go), the order does not need to change — the detectors only fire if the relevant tool is on `PATH`, so a Python-only image simply never reaches the `npm`/`cargo`/`go` detectors.

---

## Wire Protocol Addition

Add a `detected` field to the existing `ready` message so callers can see which detector fired (or that none did):

```json
{
  "type": "ready",
  "workdir": "/workspace",
  "detected": "requirements.txt",
  "duration_ms": 8421
}
```

`detected` values: the matched filename (`pyproject.toml`, `requirements.txt`, `package.json`, `Cargo.toml`, `go.mod`, `Gemfile`), `drover.yaml` when the parent RFC's explicit-config path was taken, or `none` when nothing was found.

Add `detected_bootstrap TEXT` to the `containers` table and surface it on `GET /containers/{id}`.

---

## Executor Changes

Extend `drover_executor/bootstrap.py`:

- `detect_bootstrap(repo_dir) -> DetectedBootstrap | None`
  Scans the repo root in the order above. Returns a `DetectedBootstrap` carrying the matched filename and a synthetic `DroverConfig` (single `setup` command, no `env`, no `after_ready`). Returns `None` if nothing matched.
- `on_init` flow: `load_drover_config` → if `None`, call `detect_bootstrap` → if still `None`, skip straight to `ready`.

The detector does not inspect file contents beyond the `pyproject.toml` dev-extras probe. No shell invocation, no subprocesses during detection itself.

---

## Security and Safety

- Detection runs inside the gVisor sandbox (for non-privileged images) like any other bootstrap command. No new surface.
- The `pyproject.toml` dev-extras probe reads the file with a TOML parser, not a shell or a build backend — a hostile `pyproject.toml` cannot execute code during detection.
- The install commands themselves (`pip install .`, `npm ci`, etc.) do run untrusted code from the cloned repo. This is the same trust model as a user-authored `drover.yaml` `setup` command and does not worsen it.

---

## Open Questions

1. **Should a `drover.yaml` presence silently override detection, or should a warning fire if both exist?** Proposed: `drover.yaml` wins silently — it is explicit by definition and mixing the two is confusing.
2. **Should `package.json`'s `engines.node` (or similar) trigger a toolchain swap?** Proposed: no. Toolchain management is out of scope; the operator picks the right base image.
3. **Should detectors be individually toggleable via `env` (e.g. `DROVER_AUTO_DETECT=requirements,pyproject`)?** Probably not in v1 — the opt-out is "write a `drover.yaml` with an empty `setup`."
