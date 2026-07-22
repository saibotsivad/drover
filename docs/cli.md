# Drover CLI

End-user usage reference for the `drover` command-line client. Read this
when you want to drive the orchestrator from a terminal or a script:
listing images, launching and tearing down micro-containers, and running
exec commands. Everything the CLI prints on success is JSON, so it composes
with `jq` and standard shell plumbing.

For how to **install** the binary, see the
[Installation section of `docs/releases.md`](releases.md#installation) — it
is not duplicated here.

---

## 1. Configuration

The CLI authenticates entirely through two environment variables. There is
no config file.

| Variable | Required | Description |
|---|---|---|
| `DROVER_API_URL` | yes | Base URL of the orchestrator, e.g. `https://drover.example.com`. Must be an `http`/`https` URL with a host. |
| `DROVER_API_KEY` | yes | The plain-text API key. Sent as `Authorization: Bearer <key>` on every request (and on the exec WebSocket). |

```sh
export DROVER_API_URL=https://drover.example.com
export DROVER_API_KEY=sk-...
```

If either variable is missing or malformed, the command fails fast with
exit code 2 and an error object on stderr, before any network call:

```sh
$ drover ps
{"error":"missing_configuration","detail":"DROVER_API_URL is not set"}
```

`drover --version` prints the build version, commit, and date.
`drover --help` (or `drover <command> --help`) prints usage.

---

## 2. Output contract

Every **control-plane** command prints exactly **one** JSON value to stdout
on success — the orchestrator's response, passed through verbatim so the
shape can grow without breaking `jq`-based callers.

Errors print a single JSON object to **stderr** and exit non-zero:

```json
{"error":"...","detail":"..."}
```

`detail` is omitted when there is nothing to add; timeout and lifecycle
failures also carry `id` and `status`. The two streams are kept separate so
a script can capture stdout (the result) without the error envelope leaking
into it.

`drover exec` is the one exception to "one JSON value": it streams
newline-delimited JSON frames (see [§5](#5-exec-streaming)).

---

## 3. Commands

| Command | HTTP | Description |
|---|---|---|
| `drover images` | `GET /images` | List available Drover-managed images. |
| `drover image <name>` | `GET /images/{name}` | Show details for one image. |
| `drover ps` | `GET /containers` | List micro-containers. |
| `drover start <image>` | `POST /containers` | Launch a micro-container from a managed image. |
| `drover stop <id>` | `POST /containers/{id}/stop` | Stop a container (resumable). |
| `drover destroy <id>` | `DELETE /containers/{id}` | Stop and destroy a container. |
| `drover exec <id> -- <cmd...>` | `POST /containers/{id}/execs` then `GET /containers/{id}/ws` | Run a command in a container and stream its output. |
| `drover keygen` | _(local)_ | Generate a new API key and its SHA-256 hash. |

### Read-only commands

`images`, `image <name>`, and `ps` make a single request and print the
orchestrator's JSON response unchanged.

```sh
drover images | jq -r '.[].name'
drover image python-runner
drover ps | jq -r '.[] | select(.status=="running") | .id'
```

### Lifecycle commands

`start`, `stop`, and `destroy` make their transition request and then
**block until the container reaches its terminal state** (`running`,
`stopped`, or `destroyed` respectively) before printing the final container
JSON. See [§4](#4-lifecycle-polling) for the polling semantics.

`drover start` flags:

| Flag | Default | Description |
|---|---|---|
| `--privileged` | off | Run as a privileged micro-container. |
| `--label <s>` | _(none)_ | Arbitrary label string attached to the container. |
| `--env KEY=VALUE` | _(none)_ | Set an environment variable. Repeatable; a value without `=` or with an empty key is rejected. |
| `--timeout <secs>` | `0` | Server-side container lifetime cap in seconds. `0` means the server default. |
| `--no-wait` | off | Return the transitional state immediately instead of blocking. |
| `--interval <secs>` | `1` | Seconds between poll requests while waiting. |

`drover stop` and `drover destroy` flags:

| Flag | Default | Description |
|---|---|---|
| `--no-wait` | off | Return as soon as the transition is accepted. |
| `--interval <secs>` | `1` | Seconds between poll requests while waiting. |

```sh
id=$(drover start python-runner --env FOO=bar --timeout 600 | jq -r '.id')
drover stop "$id"
drover destroy "$id"

# Fire-and-forget: don't block on the transition.
drover start python-runner --no-wait
```

---

## 4. Lifecycle polling

By default a lifecycle command blocks until the container reaches its
terminal state, polling `GET /containers/{id}` every `--interval` seconds.
The deadline for that polling is the orchestrator's
`transition_timeout_seconds` value from the initial transition response — the
CLI does not invent its own timeout.

| Situation | Behaviour |
|---|---|
| Terminal state reached | Prints the final container JSON, exit 0. |
| `--no-wait` | Prints the transitional state (e.g. `initializing`, `stopping`) immediately, exit 0; no polling. |
| `transition_timeout_seconds` is `null` | Prints a warning object to stderr and returns the transitional state without waiting. |
| Deadline elapses before terminal state | `{"error":"timeout","id":"...","status":"..."}`, exit 3. |
| `drover start` ends in `error` | `{"error":"start_failed","id":"...","status":"error"}`, exit 4. |
| Ctrl-C while waiting | `{"error":"interrupted","id":"..."}`, exit 130. |

The null-timeout warning looks like:

```json
{"warning":"orchestrator returned no transition_timeout_seconds; returning without waiting"}
```

---

## 5. Exec streaming

`drover exec <id> -- <cmd...>` runs a command inside a running container and
streams its output. Everything after the `--` separator is forwarded
**verbatim** as the command string; flags placed *before* the container id
are still parsed normally (an unknown one is rejected).

A bare `drover exec <id>` with no `--` is an error:

```sh
$ drover exec abc123
{"error":"interactive_exec_unsupported","detail":"interactive exec is not yet supported; use: drover exec <container-id> -- <command...>"}
```

Under the hood the CLI POSTs to `/containers/{id}/execs` to obtain a
`command_id`, then opens the per-container WebSocket `/containers/{id}/ws`,
filters frames to that `command_id`, and writes each matching frame to
stdout as one line of JSON — **no reshaping**. When the command's
`status:complete` frame arrives, the CLI exits with that command's
`exit_code` (0–255). See
[`docs/exec-commands.md`](exec-commands.md) for the orchestrator-side flow.

Because the CLI does not demultiplex the streams, reconstruct stdout (or
stderr) yourself with `jq`:

```sh
# Just stdout, as the command produced it:
drover exec "$id" -- ls -la | jq -r 'select(.type=="output" and .stream=="stdout") | .data'

# Capture and act on the exec exit code:
drover exec "$id" -- ./run-tests.sh | jq -r 'select(.type=="output") | .data'
echo "exit: $?"
```

Ctrl-C closes the socket cleanly and exits 130.

---

## 6. Key generation

`drover keygen` generates a random API key and prints it in three ready-to-use
forms — no network connection or environment variables required:

```
Drover Server Key:
    DROVER_API_KEY=<sha256-hash>

Drover API Header:
    Authorization: Bearer <plain-text-key>

Drover CLI Key:
    export DROVER_API_KEY=<plain-text-key>
```

Copy the **Server Key** line to the orchestrator's environment. Copy the
**CLI Key** line to your shell profile (or wherever you set `DROVER_API_KEY`
for the CLI). Use the **API Header** line when making raw HTTP requests.

See [Authentication](authentication.md) for the full setup flow.

---

## 7. Exit codes

The exit codes are a fixed contract — scripts can rely on them without
parsing stderr.

| Code | Meaning |
|---|---|
| `0` | Success. |
| `0`–`255` | `drover exec` propagates the remote command's `exit_code`. |
| `1` | Generic API error (a 4xx/5xx response, or a transport failure). |
| `2` | Missing or invalid `DROVER_API_URL` / `DROVER_API_KEY`. |
| `3` | Polling timeout on a lifecycle command (`start` / `stop` / `destroy`). |
| `4` | `drover start` ended in the `error` state. |
| `130` | Interrupted with SIGINT (Ctrl-C). |
