"""Unit tests for ExecSubscriptionManager."""

import asyncio

import pytest

from orchestrator.exec_subscription_manager import ExecSubscriptionManager

CONTAINER_ID = "container-1"
CMD_ID = "cmd-1"


@pytest.fixture
def mgr():
    return ExecSubscriptionManager()


# ---------------------------------------------------------------------------
# register / unregister
# ---------------------------------------------------------------------------


async def test_register_creates_subscriber(mgr):
    q = asyncio.Queue()
    mgr.register(CONTAINER_ID, q)
    # Notification should reach the queue
    mgr.notify_output(CONTAINER_ID, CMD_ID, "stdout", "hi\n")
    assert not q.empty()


async def test_unregister_stops_notifications(mgr):
    q = asyncio.Queue()
    mgr.register(CONTAINER_ID, q)
    mgr.unregister(CONTAINER_ID, q)
    mgr.notify_output(CONTAINER_ID, CMD_ID, "stdout", "hi\n")
    assert q.empty()


async def test_unregister_nonexistent_is_noop(mgr):
    q = asyncio.Queue()
    mgr.unregister(CONTAINER_ID, q)  # should not raise


async def test_unregister_cleans_empty_set(mgr):
    q = asyncio.Queue()
    mgr.register(CONTAINER_ID, q)
    mgr.unregister(CONTAINER_ID, q)
    assert CONTAINER_ID not in mgr._subscribers


# ---------------------------------------------------------------------------
# notify_output
# ---------------------------------------------------------------------------


async def test_notify_output_message_shape(mgr):
    q = asyncio.Queue()
    mgr.register(CONTAINER_ID, q)
    mgr.notify_output(CONTAINER_ID, CMD_ID, "stdout", "hello\n")
    msg = q.get_nowait()
    assert msg == {
        "type": "output",
        "command_id": CMD_ID,
        "stream": "stdout",
        "data": "hello\n",
    }


async def test_notify_output_fanout(mgr):
    q1, q2 = asyncio.Queue(), asyncio.Queue()
    mgr.register(CONTAINER_ID, q1)
    mgr.register(CONTAINER_ID, q2)
    mgr.notify_output(CONTAINER_ID, CMD_ID, "stderr", "err\n")
    assert not q1.empty()
    assert not q2.empty()


async def test_notify_output_no_subscribers_is_noop(mgr):
    # Should not raise
    mgr.notify_output(CONTAINER_ID, CMD_ID, "stdout", "data")


async def test_notify_output_different_containers_isolated(mgr):
    q1, q2 = asyncio.Queue(), asyncio.Queue()
    mgr.register("container-a", q1)
    mgr.register("container-b", q2)
    mgr.notify_output("container-a", CMD_ID, "stdout", "data")
    assert not q1.empty()
    assert q2.empty()


# ---------------------------------------------------------------------------
# notify_result
# ---------------------------------------------------------------------------


async def test_notify_result_message_shape(mgr):
    q = asyncio.Queue()
    mgr.register(CONTAINER_ID, q)
    mgr.notify_result(CONTAINER_ID, CMD_ID, 0)
    msg = q.get_nowait()
    assert msg == {"type": "result", "command_id": CMD_ID, "exit_code": 0}


async def test_notify_result_nonzero_exit_code(mgr):
    q = asyncio.Queue()
    mgr.register(CONTAINER_ID, q)
    mgr.notify_result(CONTAINER_ID, CMD_ID, 127)
    msg = q.get_nowait()
    assert msg["exit_code"] == 127


async def test_notify_result_fanout(mgr):
    q1, q2 = asyncio.Queue(), asyncio.Queue()
    mgr.register(CONTAINER_ID, q1)
    mgr.register(CONTAINER_ID, q2)
    mgr.notify_result(CONTAINER_ID, CMD_ID, 0)
    assert not q1.empty()
    assert not q2.empty()


# ---------------------------------------------------------------------------
# Multiple commands on the same container
# ---------------------------------------------------------------------------


async def test_multiple_commands_share_same_queue(mgr):
    q = asyncio.Queue()
    mgr.register(CONTAINER_ID, q)
    mgr.notify_output(CONTAINER_ID, "cmd-a", "stdout", "first\n")
    mgr.notify_output(CONTAINER_ID, "cmd-b", "stdout", "second\n")
    msg1 = q.get_nowait()
    msg2 = q.get_nowait()
    assert msg1["command_id"] == "cmd-a"
    assert msg2["command_id"] == "cmd-b"
