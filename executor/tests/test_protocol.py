"""Tests for drover_executor.protocol — encode/decode all message types."""

import json

import pytest

from drover_executor.protocol import (
    decode,
    encode_done,
    encode_heartbeat,
    encode_output,
    encode_result,
)


class TestEncodeHeartbeat:
    def test_format(self):
        raw = encode_heartbeat()
        assert raw.endswith(b"\n")
        msg = json.loads(raw)
        assert msg == {"type": "heartbeat"}

    def test_bytes_type(self):
        assert isinstance(encode_heartbeat(), bytes)


class TestEncodeOutput:
    def test_format(self):
        raw = encode_output("cmd-1", "stdout", "hello world")
        msg = json.loads(raw)
        assert msg == {
            "type": "output",
            "id": "cmd-1",
            "stream": "stdout",
            "data": "hello world",
        }

    def test_stderr_stream(self):
        raw = encode_output("cmd-2", "stderr", "err")
        msg = json.loads(raw)
        assert msg["stream"] == "stderr"

    def test_empty_data(self):
        raw = encode_output("cmd-3", "stdout", "")
        msg = json.loads(raw)
        assert msg["data"] == ""

    def test_unicode_data(self):
        raw = encode_output("cmd-4", "stdout", "hello \u2603 world \U0001f600")
        msg = json.loads(raw)
        assert "\u2603" in msg["data"]
        assert "\U0001f600" in msg["data"]

    def test_newline_in_data(self):
        raw = encode_output("cmd-5", "stdout", "line1\nline2\n")
        msg = json.loads(raw)
        assert msg["data"] == "line1\nline2\n"
        # The outer message itself is a single JSON line
        assert raw.count(b"\n") == 1


class TestEncodeResult:
    def test_format(self):
        raw = encode_result("cmd-1", 0)
        msg = json.loads(raw)
        assert msg == {"type": "result", "id": "cmd-1", "exit_code": 0}

    def test_nonzero_exit(self):
        raw = encode_result("cmd-2", 137)
        msg = json.loads(raw)
        assert msg["exit_code"] == 137


class TestEncodeDone:
    def test_format(self):
        raw = encode_done()
        msg = json.loads(raw)
        assert msg == {"type": "done"}


class TestDecode:
    def test_command_message(self):
        line = b'{"type": "command", "id": "abc", "exec": "echo hi"}\n'
        msg = decode(line)
        assert msg == {"type": "command", "id": "abc", "exec": "echo hi"}

    def test_heartbeat_message(self):
        msg = decode(b'{"type": "heartbeat"}\n')
        assert msg == {"type": "heartbeat"}

    def test_strips_trailing_whitespace(self):
        msg = decode(b'{"type": "done"}  \n')
        assert msg == {"type": "done"}

    def test_unicode(self):
        line = json.dumps({"type": "output", "data": "\u2603"}).encode() + b"\n"
        msg = decode(line)
        assert msg["data"] == "\u2603"

    def test_empty_line_raises(self):
        with pytest.raises(ValueError, match="Empty line"):
            decode(b"")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="Empty line"):
            decode(b"   \n")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            decode(b"not json\n")

    def test_truncated_json_raises(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            decode(b'{"type": "heartbeat"\n')
