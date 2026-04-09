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

- [ ] `run_command(cmd_id, exec_str, send_fn, chunk_size=8192) -> int`
  - `asyncio.create_subprocess_shell` with stdout/stderr PIPE
  - Two concurrent tasks reading stdout and stderr via `pipe.read(chunk_size)`
  - Each chunk sent immediately via `send_fn` (an async callable)
  - Decode bytes to str with `errors="replace"`
  - Return exit code after both streams close and process exits

### `agent.py` — core agent

- [ ] `Agent.__init__(socket_path, heartbeat_interval, max_concurrent_commands)`
  - `socket_path` default `/run/orchestrator.sock`
  - `heartbeat_interval` default `2.0` seconds
  - `max_concurrent_commands` default `None` (unlimited); when set, use an `asyncio.Semaphore` to cap concurrent subprocesses
- [ ] `Agent.run()` — main entry point
  - Connect via `asyncio.open_unix_connection`
  - Call `on_connect()`
  - Start heartbeat background task
  - Read loop: decode each line, dispatch `command` messages
  - On disconnect: cancel tasks, call `on_disconnect()`
  - Register signal handlers (SIGTERM, SIGINT) for graceful shutdown
- [ ] `Agent.send_done()` — send done signal (for custom agents)
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
  - `--log-level` (default `INFO`)
- [ ] Configure `logging.basicConfig` to stderr

### Tests

- [ ] `test_protocol.py` — encode/decode all message types, unicode, empty data, invalid JSON
- [ ] `test_runner.py` — real subprocesses (`echo`, `cat`, `false`), stdout/stderr streaming, exit codes, chunk boundaries
- [ ] `test_agent.py` — full lifecycle against a mock socket server: connection, heartbeat timing, command dispatch + output, concurrent commands, write serialization, graceful shutdown, max-concurrent-commands semaphore
- [ ] Add `executor/tests/` to CI workflow (`test.yml`)

---

## Design Decisions

### Concurrency

Multiple commands can run simultaneously. Each command gets its own `asyncio.Task`. The `max_concurrent_commands` setting (optional) gates command execution through an `asyncio.Semaphore` — when the limit is reached, new commands wait until a slot opens. The heartbeat and socket reader are not affected by the semaphore.

### Output streaming

Subprocess stdout and stderr are read in 8 KB chunks and sent immediately as `output` messages. No line buffering — chunks are sent as-is, which handles both line-oriented output and progress bars / binary-ish output. Bytes are decoded to str with `errors="replace"`.

### Done signal

In default CLI mode, the agent runs indefinitely, processing commands and heartbeating until the orchestrator closes the socket. The `send_done()` method exists for custom agents that know when their work is complete and want to trigger an early stop.

### Graceful shutdown

On SIGTERM/SIGINT: cancel all running command tasks (which kills subprocesses), send `done` signal, close socket connection. If the orchestrator closes the socket first (normal stop flow), the reader returns empty and the agent exits cleanly.

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
