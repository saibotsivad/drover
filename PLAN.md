# Plan: `drover_executor` Library

Standalone guest-agent library that runs inside Drover micro-containers. Connects to the orchestrator's Unix socket, receives commands, executes them as subprocesses, streams stdout/stderr back, and reports exit codes.

Lives at `executor/` in the repo root. Zero external dependencies (Python 3.12+ stdlib only).

> **Not published to PyPI yet.** Install directly from the repo:
> ```
> pip install "git+https://github.com/saibotsivad/drover.git@v1.0.0#subdirectory=executor"
> ```

---

## File Structure

```
executor/
├── pyproject.toml
├── drover_executor/
│   ├── __init__.py
│   ├── __main__.py
│   ├── agent.py
│   ├── protocol.py
│   └── runner.py
└── tests/
    ├── __init__.py
    ├── test_protocol.py
    ├── test_runner.py
    └── test_agent.py
```

---

## Checklist

### Package scaffolding

- [ ] Create `executor/pyproject.toml` — package name `drover-executor`, import name `drover_executor`, Python >=3.12, zero dependencies, console script entry point `drover-executor`
- [ ] Create `executor/drover_executor/__init__.py` — export `Agent` class

### `protocol.py` — wire protocol

Encodes/decodes newline-delimited JSON matching `orchestrator/socket_manager.py`.

- [ ] `encode_heartbeat() -> bytes` — `{"type": "heartbeat"}\n`
- [ ] `encode_output(id, stream, data) -> bytes` — `{"type": "output", ...}\n`
- [ ] `encode_result(id, exit_code) -> bytes` — `{"type": "result", ...}\n`
- [ ] `encode_done() -> bytes` — `{"type": "done"}\n`
- [ ] `decode(line: bytes) -> dict` — parse a JSON line, raise on invalid input

### `runner.py` — subprocess execution

- [ ] `run_command(exec_str, send_fn, chunk_size=8192) -> int`
  - `asyncio.create_subprocess_shell` with stdout/stderr PIPE and **stdin=DEVNULL** (prevents commands from hanging on stdin reads, e.g. `git` credential prompts or `apt` confirmation prompts)
  - Two concurrent tasks reading stdout and stderr via `pipe.read(chunk_size)`
  - Each chunk sent immediately via `send_fn` (an async callable with signature `async def(stream: str, data: str)`)
  - Decode bytes to str with `errors="replace"`
  - Return exit code after both streams close and process exits
  - **Subprocess cleanup on cancellation**: asyncio task cancellation does NOT automatically kill child processes — they continue running as orphans. The runner must use `try/finally` with `proc.kill()` + `await proc.wait()` on `CancelledError` to ensure subprocesses are cleaned up

The runner does not know about the wire protocol or command IDs. The agent passes a closure already bound to the command ID:

```python
# in agent.on_command:
async def send_output(stream: str, data: str) -> None:
    await self._send(protocol.encode_output(cmd_id, stream, data))

exit_code = await run_command(exec_str, send_output)
```

This keeps the runner focused on subprocess management and the agent responsible for protocol framing.

### `agent.py` — core agent

- [ ] `Agent.__init__(socket_path, heartbeat_interval, max_concurrent_commands, auto_heartbeat)`
  - `socket_path` default `/run/orchestrator.sock`
  - `heartbeat_interval` default `2.0` seconds
  - `max_concurrent_commands` default `None` (unlimited); when set, use an `asyncio.Semaphore` to cap concurrent subprocesses
  - `auto_heartbeat` default `True`; when `True`, a background task sends heartbeats unconditionally for the lifetime of the connection; when `False`, no automatic heartbeats are sent and the caller is responsible for calling `send_heartbeat()` on their own schedule
- [ ] `Agent.run()` — main entry point
  - Connect via `asyncio.open_unix_connection`
  - Call `on_connect()`
  - Start heartbeat background task
  - Read loop: decode each line, dispatch `command` messages
  - On disconnect: cancel tasks, call `on_disconnect()`
  - Register signal handlers (SIGTERM, SIGINT) for graceful shutdown
- [ ] `Agent.send_done()` — send done signal (for custom agents)
- [ ] `Agent.send_heartbeat()` — send a single heartbeat; used manually when `auto_heartbeat=False`
- [ ] `Agent.on_connect()` — override point, default no-op
- [ ] `Agent.on_command(cmd_id, exec_str)` — override point, default calls `runner.run_command` and sends output/result messages
- [ ] `Agent.on_disconnect()` — override point, default no-op
- [ ] Write lock (`asyncio.Lock`) on all socket writes so concurrent command output doesn't interleave partial JSON lines

### `__main__.py` — CLI entry point

- [ ] `python -m drover_executor` runs the default agent
- [ ] CLI args via `argparse`:
  - `--socket` (default `/run/orchestrator.sock`)
  - `--heartbeat-interval` (default `2.0`)
  - `--max-concurrent-commands` (default unlimited)
  - `--no-auto-heartbeat` (flag to disable automatic heartbeats)
  - `--log-level` (default `INFO`)
- [ ] Configure `logging.basicConfig` to stderr

### Tests

- [ ] `test_protocol.py` — encode/decode all message types, unicode, empty data, invalid JSON
- [ ] `test_runner.py` — real subprocesses (`echo`, `cat`, `false`), stdout/stderr streaming, exit codes, chunk boundaries
- [ ] `test_agent.py` — full lifecycle against a mock socket server: connection, heartbeat timing, command dispatch + output, concurrent commands, write serialization, graceful shutdown, max-concurrent-commands semaphore, auto_heartbeat on/off. Tests use `pytest`'s `tmp_path` fixture for socket files (can't use `/run/orchestrator.sock` in CI)
- [ ] Add `executor/tests/` to CI workflow (`test.yml`)

---

## Design Decisions

### Concurrency

Multiple commands can run simultaneously. Each command gets its own `asyncio.Task`. The `max_concurrent_commands` setting (optional) gates command execution through an `asyncio.Semaphore` — when the limit is reached, new commands wait until a slot opens. The heartbeat and socket reader are not affected by the semaphore.

### Output streaming

Subprocess stdout and stderr are read in 8 KB chunks and sent immediately as `output` messages. No line buffering — chunks are sent as-is, which handles both line-oriented output and progress bars / binary-ish output. Bytes are decoded to str with `errors="replace"`.

### Done signal

In default CLI mode, the agent runs indefinitely, processing commands and heartbeating until the orchestrator closes the socket. The `send_done()` method exists for custom agents that know when their work is complete and want to trigger an early stop.

### Subprocess lifecycle

**Stdin**: All subprocesses are started with `stdin=DEVNULL`. This prevents commands from hanging when they try to read input (e.g. `git` credential prompts, `apt` confirmation prompts). Commands that need stdin should fail fast rather than block silently.

**Cancellation (asyncio footgun)**: Cancelling an asyncio task that is awaiting `proc.stdout.read()` or `proc.wait()` will cancel the Python coroutine but does **not** terminate the child process. The subprocess continues running as an orphan. The runner must explicitly handle this:

```python
proc = await asyncio.create_subprocess_shell(...)
try:
    # ... read stdout/stderr, await proc.wait() ...
except asyncio.CancelledError:
    proc.kill()
    await proc.wait()
    raise
```

This is critical for graceful shutdown — without it, `docker stop` would leave zombie processes inside the container.

### Graceful shutdown

On SIGTERM/SIGINT: cancel all running command tasks (runner's `CancelledError` handler kills subprocesses), send `done` signal, close socket connection. If the orchestrator closes the socket first (normal stop flow), the reader returns empty and the agent exits cleanly.

### Heartbeat modes

Heartbeat behavior is configurable via `auto_heartbeat` (default `True`):

- **`auto_heartbeat=True`**: A background task sends heartbeats unconditionally for the lifetime of the connection. This keeps the container alive even when no commands are running or when a command is hung. The orchestrator's idle-timeout reaper never fires as long as the agent is connected. This is the right default for most use cases — the caller is responsible for destroying the container if a command takes too long.

- **`auto_heartbeat=False`**: No automatic heartbeats. The library user calls `send_heartbeat()` on their own schedule, giving them full control over when the container is considered "alive." This is useful for custom agents that want the container to be reaped if their own logic stalls — e.g., only heartbeating while actively processing work.

### No reconnection

The orchestrator creates the socket before starting the container. If the socket isn't there at startup, the agent fails fast. If the connection drops mid-session, the container is being stopped — no retry logic needed.

### Extensibility

Image builders subclass `Agent` and override `on_connect`, `on_command`, or `on_disconnect`:

```python
import asyncio
from drover_executor import Agent

class MyAgent(Agent):
    async def on_connect(self):
        # custom setup
        pass

    async def on_command(self, cmd_id, exec_str):
        # custom handling, or call super() for default shell execution
        await super().on_command(cmd_id, exec_str)

asyncio.run(MyAgent().run())
```
