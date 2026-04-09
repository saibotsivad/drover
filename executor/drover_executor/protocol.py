"""Wire protocol for newline-delimited JSON messages.

Matches the format defined in orchestrator/socket_manager.py.

Guest -> Orchestrator:
  - heartbeat: {"type": "heartbeat"}
  - output:    {"type": "output", "id": "<cmd_id>", "stream": "stdout|stderr", "data": "..."}
  - result:    {"type": "result", "id": "<cmd_id>", "exit_code": N}
  - done:      {"type": "done"}

Orchestrator -> Guest:
  - command:   {"type": "command", "id": "<cmd_id>", "exec": "..."}
"""

import json


def encode_heartbeat() -> bytes:
    """Encode a heartbeat message."""
    return json.dumps({"type": "heartbeat"}).encode() + b"\n"


def encode_output(cmd_id: str, stream: str, data: str) -> bytes:
    """Encode a command output message."""
    return json.dumps({
        "type": "output",
        "id": cmd_id,
        "stream": stream,
        "data": data,
    }).encode() + b"\n"


def encode_result(cmd_id: str, exit_code: int) -> bytes:
    """Encode a command result message."""
    return json.dumps({
        "type": "result",
        "id": cmd_id,
        "exit_code": exit_code,
    }).encode() + b"\n"


def encode_done() -> bytes:
    """Encode a done signal."""
    return json.dumps({"type": "done"}).encode() + b"\n"


def decode(line: bytes) -> dict:
    """Decode a JSON line into a message dict.

    Raises ValueError on empty input or invalid JSON.
    """
    if not line.strip():
        raise ValueError("Empty line")
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
