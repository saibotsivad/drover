"""Per-container WebSocket output queue registry.

Each connected WebSocket gets its own ``asyncio.Queue``.  The
``SocketManager`` and the Docker-log streaming background task both
broadcast into every queue registered for the container they belong to;
the WebSocket handler is the single consumer for each queue, so
concurrent writes from the two producers can't race with the socket
send.

Slow consumers don't backpressure the SocketManager: when a queue is
full we drop the message for that connection rather than blocking the
guest-agent read loop.
"""

import asyncio


class ConnectionManager:
    """Manages per-container WebSocket output queues."""

    def __init__(self) -> None:
        # container_id -> set of queues (one per connected WebSocket)
        self._queues: dict[str, set[asyncio.Queue]] = {}

    def connect(self, container_id: str) -> asyncio.Queue:
        """Register a new WebSocket connection; return its dedicated queue."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues.setdefault(container_id, set()).add(queue)
        return queue

    def disconnect(self, container_id: str, queue: asyncio.Queue) -> None:
        """Remove a disconnected WebSocket's queue."""
        queues = self._queues.get(container_id)
        if queues:
            queues.discard(queue)
            if not queues:
                del self._queues[container_id]

    async def broadcast(self, container_id: str, message: dict) -> None:
        """Put a message into every queue registered for *container_id*.

        Uses ``put_nowait`` and drops the message for queues that are full
        (slow consumer) rather than blocking the SocketManager's read
        loop.
        """
        for queue in list(self._queues.get(container_id, ())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # slow consumer; drop rather than block
