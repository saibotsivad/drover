"""Fan-out registry for real-time command output subscriptions.

WebSocket handlers register a queue per container connection.  When the
SocketManager receives output or result messages from the guest agent, it
notifies all registered queues so connected clients receive output in
real-time without polling.

All public methods are synchronous so they can be called from within async
``_handle_output`` / ``_handle_result`` methods without yielding.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class ExecSubscriptionManager:
    """Maps container_id to a set of asyncio Queues (one per WS connection).

    Queue message shapes:
      {"type": "output", "command_id": str, "stream": "stdout"|"stderr", "data": str}
      {"type": "result", "command_id": str, "exit_code": int}
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def register(self, container_id: str, queue: asyncio.Queue) -> None:
        """Register *queue* to receive output events for *container_id*."""
        self._subscribers.setdefault(container_id, set()).add(queue)
        logger.debug("WS subscriber registered for container %s", container_id)

    def unregister(self, container_id: str, queue: asyncio.Queue) -> None:
        """Remove *queue* from the subscriber set for *container_id*."""
        subs = self._subscribers.get(container_id)
        if subs is None:
            return
        subs.discard(queue)
        if not subs:
            del self._subscribers[container_id]
        logger.debug("WS subscriber unregistered for container %s", container_id)

    def notify_output(
        self, container_id: str, command_id: str, stream: str, data: str
    ) -> None:
        """Push an output chunk to all subscribers of *container_id*."""
        subs = self._subscribers.get(container_id)
        if not subs:
            return
        msg = {"type": "output", "command_id": command_id, "stream": stream, "data": data}
        for queue in subs:
            queue.put_nowait(msg)

    def notify_result(
        self, container_id: str, command_id: str, exit_code: int
    ) -> None:
        """Push a result (command complete) to all subscribers of *container_id*."""
        subs = self._subscribers.get(container_id)
        if not subs:
            return
        msg = {"type": "result", "command_id": command_id, "exit_code": exit_code}
        for queue in subs:
            queue.put_nowait(msg)
