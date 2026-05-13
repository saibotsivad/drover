"""Tests for the in-memory ring buffer logging handler."""

import logging

from orchestrator.log_buffer import RingBufferHandler


def _make_logger(name: str, handler: logging.Handler) -> logging.Logger:
    log = logging.getLogger(name)
    log.handlers.clear()
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log


def test_ring_buffer_captures_records():
    handler = RingBufferHandler(capacity=10)
    log = _make_logger("test_capture", handler)
    log.info("hello from container cnt_abc")
    snap = handler.snapshot()
    assert len(snap) == 1
    assert snap[0].level == "INFO"
    assert "cnt_abc" in snap[0].message


def test_ring_buffer_drops_oldest_past_capacity():
    handler = RingBufferHandler(capacity=3)
    log = _make_logger("test_capacity", handler)
    for i in range(5):
        log.info("msg %d", i)
    snap = handler.snapshot()
    assert len(snap) == 3
    assert [r.message for r in snap] == ["msg 2", "msg 3", "msg 4"]


def test_filter_contains_returns_only_matches():
    handler = RingBufferHandler(capacity=20)
    log = _make_logger("test_filter", handler)
    log.info("Container cnt_aaa created")
    log.info("Container cnt_bbb created")
    log.warning("Container cnt_aaa idle timeout")
    log.info("unrelated log line")

    matches = handler.filter_contains("cnt_aaa")
    assert len(matches) == 2
    assert all("cnt_aaa" in m.message for m in matches)
    # Order is chronological
    assert matches[0].message.endswith("created")
    assert matches[1].message.endswith("idle timeout")


def test_filter_contains_respects_limit():
    handler = RingBufferHandler(capacity=20)
    log = _make_logger("test_filter_limit", handler)
    for i in range(5):
        log.info("Container cnt_xxx event %d", i)
    matches = handler.filter_contains("cnt_xxx", limit=2)
    assert len(matches) == 2
    # Most recent matches retained
    assert matches[0].message.endswith("event 3")
    assert matches[1].message.endswith("event 4")


def test_filter_contains_empty_needle_returns_empty():
    handler = RingBufferHandler(capacity=5)
    log = _make_logger("test_empty_needle", handler)
    log.info("something")
    assert handler.filter_contains("") == []


def test_handler_records_exception_info():
    handler = RingBufferHandler(capacity=5)
    log = _make_logger("test_exc", handler)
    try:
        raise ValueError("oops cnt_xyz")
    except ValueError:
        log.exception("failed to do thing for cnt_xyz")
    matches = handler.filter_contains("cnt_xyz")
    assert len(matches) == 1
    assert "ValueError" in matches[0].message
    assert "oops cnt_xyz" in matches[0].message
