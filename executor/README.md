# drover-executor

Guest-agent library that runs inside Drover micro-containers. It connects to the orchestrator's Unix socket at `/run/orchestrator.sock`, receives commands, executes them as subprocesses, streams stdout/stderr back, and reports exit codes.

Zero external dependencies — Python 3.11+ stdlib only.

---

## Adding to a Docker Image

Install directly from the repo in your Dockerfile:

```dockerfile
FROM python:3.12-slim

# Install the executor
RUN pip install --no-cache-dir \
    "git+https://github.com/saibotsivad/drover.git@main#subdirectory=executor"

# ... install your application dependencies ...

# Run the default agent on container startup
CMD ["drover-executor"]
```

The `drover-executor` command connects to `/run/orchestrator.sock` (the socket the orchestrator bind-mounts into every container) and processes commands indefinitely.

### Pinning a version

Once a release is tagged, pin to it:

```dockerfile
RUN pip install --no-cache-dir \
    "git+https://github.com/saibotsivad/drover.git@v1.0.0#subdirectory=executor"
```

---

## CLI Options

```
drover-executor [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--socket PATH` | `/run/orchestrator.sock` | Path to the orchestrator Unix socket |
| `--heartbeat-interval SECONDS` | `2.0` | Seconds between automatic heartbeats |
| `--max-concurrent-commands N` | unlimited | Cap on concurrent subprocesses |
| `--no-auto-heartbeat` | _(off)_ | Disable automatic heartbeats |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

You can also run it as a module:

```
python -m drover_executor --log-level DEBUG
```

In a Dockerfile, pass options via the `CMD` instruction:

```dockerfile
CMD ["drover-executor", "--heartbeat-interval", "5", "--max-concurrent-commands", "4", "--log-level", "DEBUG"]
```

---

## Custom Agents

For images that need more than shell command execution, subclass `Agent` and override the hooks:

```python
import asyncio
from drover_executor import Agent

class MyAgent(Agent):
    async def on_connect(self):
        # Called after the socket connection is established
        print("Connected to orchestrator")

    async def on_command(self, cmd_id, exec_str):
        # Custom command handling — or call super() for default shell execution
        if exec_str.startswith("custom:"):
            # ... do custom work, send output/result manually ...
            pass
        else:
            await super().on_command(cmd_id, exec_str)

    async def on_disconnect(self):
        # Called after the socket connection is closed
        print("Disconnected")

asyncio.run(MyAgent().run())
```

### Agent API

| Method | Description |
|---|---|
| `Agent(socket_path, heartbeat_interval, max_concurrent_commands, auto_heartbeat)` | Constructor with sensible defaults |
| `await agent.run()` | Connect and process commands until the socket closes or a signal is received |
| `await agent.send_done()` | Tell the orchestrator this container is finished (triggers early stop) |
| `await agent.send_heartbeat()` | Send a single heartbeat manually (useful with `auto_heartbeat=False`) |
| `async on_connect()` | Override point, called after connection |
| `async on_command(cmd_id, exec_str)` | Override point, called for each command |
| `async on_disconnect()` | Override point, called after disconnection |

### Heartbeat Modes

- **`auto_heartbeat=True`** (default): A background task sends heartbeats for the lifetime of the connection. The container stays alive as long as the agent is running.
- **`auto_heartbeat=False`**: No automatic heartbeats. Call `send_heartbeat()` on your own schedule. This is useful when you want the orchestrator's idle timeout to reap the container if your agent stalls.

### Done Signal

In default mode, the agent runs indefinitely until the orchestrator closes the socket. Call `send_done()` when your custom agent finishes its work to trigger an immediate stop instead of waiting for the idle timeout.

---

## Dockerfile Examples

### Minimal image

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir \
    "git+https://github.com/saibotsivad/drover.git@main#subdirectory=executor"
CMD ["drover-executor"]
```

### Image with application tooling

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "git+https://github.com/saibotsivad/drover.git@main#subdirectory=executor"

CMD ["drover-executor"]
```

### Image with a custom agent

```dockerfile
FROM python:3.12-slim

RUN pip install --no-cache-dir \
    "git+https://github.com/saibotsivad/drover.git@main#subdirectory=executor"

COPY my_agent.py /app/my_agent.py

CMD ["python", "/app/my_agent.py"]
```

Where `my_agent.py` is:

```python
import asyncio
from drover_executor import Agent

class MyAgent(Agent):
    async def on_connect(self):
        # Run setup tasks, then signal done
        await super().on_command("setup", "apt-get update")
        await self.send_done()

asyncio.run(MyAgent().run())
```
