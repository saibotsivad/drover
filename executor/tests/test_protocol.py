"""Tests for drover_executor.protocol — encode/decode all message types.

Pure unit tests for the wire protocol layer.  Each encoder must produce
valid newline-delimited JSON (exactly one ``\\n`` terminator per message,
all fields present), and the decoder must round-trip cleanly as well as
reject malformed input.

These tests validate the contract between the executor and the
orchestrator's ``socket_manager.py``, which speaks the same protocol.
"""

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
        """Heartbeat is a single JSON line with type field only."""
        raw = encode_heartbeat()
        assert raw.endswith(b"\n")
        msg = json.loads(raw)
        assert msg == {"type": "heartbeat"}

    def test_bytes_type(self):
        """All encoders return bytes, ready to write to the socket."""
        assert isinstance(encode_heartbeat(), bytes)


class TestEncodeOutput:
    def test_format(self):
        """Output message includes id, stream name, and data payload."""
        raw = encode_output("cmd-1", "stdout", "hello world")
        msg = json.loads(raw)
        assert msg == {
            "type": "output",
            "id": "cmd-1",
            "stream": "stdout",
            "data": "hello world",
        }

    def test_stderr_stream(self):
        """Stream field distinguishes stdout from stderr."""
        raw = encode_output("cmd-2", "stderr", "err")
        msg = json.loads(raw)
        assert msg["stream"] == "stderr"

    def test_empty_data(self):
        """Empty data payloads are valid (e.g. flush with no content)."""
        raw = encode_output("cmd-3", "stdout", "")
        msg = json.loads(raw)
        assert msg["data"] == ""

    def test_unicode_data(self):
        """Non-ASCII characters survive the JSON encode/decode cycle."""
        raw = encode_output("cmd-4", "stdout", "hello \u2603 world \U0001f600")
        msg = json.loads(raw)
        assert "\u2603" in msg["data"]
        assert "\U0001f600" in msg["data"]

    def test_newline_in_data(self):
        """Newlines in the data payload are JSON-escaped, keeping the
        message itself on a single line (critical for the wire protocol)."""
        raw = encode_output("cmd-5", "stdout", "line1\nline2\n")
        msg = json.loads(raw)
        assert msg["data"] == "line1\nline2\n"
        # The outer message itself is a single JSON line
        assert raw.count(b"\n") == 1


class TestEncodeResult:
    def test_format(self):
        """Result message includes id and integer exit code."""
        raw = encode_result("cmd-1", 0)
        msg = json.loads(raw)
        assert msg == {"type": "result", "id": "cmd-1", "exit_code": 0}

    def test_nonzero_exit(self):
        """Signal-killed processes report high exit codes (e.g. 137 = SIGKILL)."""
        raw = encode_result("cmd-2", 137)
        msg = json.loads(raw)
        assert msg["exit_code"] == 137


class TestEncodeDone:
    def test_format(self):
        """Done message has type field only, no payload."""
        raw = encode_done()
        msg = json.loads(raw)
        assert msg == {"type": "done"}


class TestDecode:
    """Decoder tests cover both valid messages and rejection of bad input."""

    def test_command_message(self):
        """Command messages (orchestrator -> agent) decode correctly."""
        line = b'{"type": "command", "id": "abc", "exec": "echo hi"}\n'
        msg = decode(line)
        assert msg == {"type": "command", "id": "abc", "exec": "echo hi"}

    def test_heartbeat_message(self):
        """Heartbeat messages round-trip through encode/decode."""
        msg = decode(b'{"type": "heartbeat"}\n')
        assert msg == {"type": "heartbeat"}

    def test_strips_trailing_whitespace(self):
        """Trailing whitespace before the newline is tolerated."""
        msg = decode(b'{"type": "done"}  \n')
        assert msg == {"type": "done"}

    def test_unicode(self):
        """Non-ASCII characters in payloads survive the round-trip."""
        line = json.dumps({"type": "output", "data": "\u2603"}).encode() + b"\n"
        msg = decode(line)
        assert msg["data"] == "\u2603"

    def test_empty_line_raises(self):
        """Empty input is rejected (not silently ignored)."""
        with pytest.raises(ValueError, match="Empty line"):
            decode(b"")

    def test_whitespace_only_raises(self):
        """Whitespace-only input is rejected as empty."""
        with pytest.raises(ValueError, match="Empty line"):
            decode(b"   \n")

    def test_invalid_json_raises(self):
        """Non-JSON input produces a clear ValueError."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            decode(b"not json\n")

    def test_truncated_json_raises(self):
        """Incomplete JSON (e.g. missing closing brace) is rejected."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            decode(b'{"type": "heartbeat"\n')
