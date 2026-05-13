"""Tests for the Docker multiplexed-log demultiplexer."""

from orchestrator.docker_client import demultiplex_docker_logs


def _frame(stream_type: int, payload: bytes) -> bytes:
    header = bytes([stream_type, 0, 0, 0]) + len(payload).to_bytes(4, "big")
    return header + payload


def test_demultiplex_empty():
    assert demultiplex_docker_logs(b"") == []


def test_demultiplex_single_stdout_frame():
    out = demultiplex_docker_logs(_frame(1, b"hello world\n"))
    assert out == [{"stream": "stdout", "data": "hello world\n"}]


def test_demultiplex_interleaved_streams():
    payload = _frame(1, b"out1\n") + _frame(2, b"err1\n") + _frame(1, b"out2\n")
    out = demultiplex_docker_logs(payload)
    assert out == [
        {"stream": "stdout", "data": "out1\n"},
        {"stream": "stderr", "data": "err1\n"},
        {"stream": "stdout", "data": "out2\n"},
    ]


def test_demultiplex_handles_invalid_utf8():
    # \xff is invalid UTF-8; should be replaced rather than raise.
    out = demultiplex_docker_logs(_frame(1, b"ok\xffend"))
    assert len(out) == 1
    assert out[0]["stream"] == "stdout"
    assert "ok" in out[0]["data"]
    assert "end" in out[0]["data"]


def test_demultiplex_truncated_last_frame_is_best_effort():
    # Header claims 100 bytes but only 5 are present.
    bad = bytes([1, 0, 0, 0]) + (100).to_bytes(4, "big") + b"hello"
    out = demultiplex_docker_logs(bad)
    assert out == [{"stream": "stdout", "data": "hello"}]


def test_demultiplex_falls_back_for_non_frame_payload():
    # A TTY-enabled container returns plain text — no valid stream-type byte
    # at position 0. The demuxer should fall back to plain-text output.
    out = demultiplex_docker_logs(b"plain text from a TTY container\n")
    assert len(out) == 1
    assert out[0]["stream"] == "stdout"
    assert out[0]["data"].startswith("plain text")
