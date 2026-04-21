# Container Initialization

When a container is created via `POST /containers`, the orchestrator returns
immediately with a status of `initializing`. The container is not considered
ready to accept exec commands until its guest agent connects to the orchestrator
socket and sends a `ready` message.

## Initialization flow

```
POST /containers
  → orchestrator inserts DB row (status: initializing)
  → background task: create socket, docker create, docker start
  → guest agent starts inside container
  → guest agent calls on_connect() [do your startup work here]
  → guest agent sends {"type": "ready"}
  → orchestrator updates status: initializing → running
```

At its most basic, the ready message can be sent from any language or shell:

```bash
echo '{"type": "ready"}' | socat - UNIX-CONNECT:/run/orchestrator.sock
```

Callers should poll `GET /containers/{id}` until `status` is `running` before
submitting exec commands. If initialization fails or times out, status
transitions to `error` with an `error_code` field explaining the cause.

## Default behavior

The `Agent` base class sends `ready` automatically after `on_connect()`
returns. For simple containers with no startup work, no configuration is
needed:

```python
from drover_executor import Agent

agent = Agent()
asyncio.run(agent.run())
```

The agent connects, `on_connect()` returns immediately (it does nothing by
default), and `ready` is sent.

## Custom startup work

Override `on_connect()` to perform startup work before the agent signals
readiness. The `ready` message is sent only after `on_connect()` completes,
so the orchestrator will not mark the container `running` until your startup
logic finishes.

```python
from drover_executor import Agent

class MyAgent(Agent):
    async def on_connect(self) -> None:
        await start_background_service()
        await wait_for_db_connection()
        # on_connect returns here → ready is sent automatically

agent = MyAgent()
asyncio.run(agent.run())
```

If `on_connect()` raises an exception, `ready` is never sent, the container
stays in `initializing`, and the orchestrator's init timeout will eventually
transition it to `error`.
