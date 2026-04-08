"""K-sortable, URL-safe, filename-safe ID generation.

Generates ULID-compatible identifiers: 10-char timestamp (ms) + 16-char
randomness = 26 Crockford base32 characters.  Character set is
[0-9A-Z] (excluding I, L, O, U) — safe for URLs, filenames, and
lexicographic sorting reflects creation order.
"""

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    chars.reverse()
    return "".join(chars)


def generate_id() -> str:
    """Return a 26-character ULID string."""
    timestamp_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(timestamp_ms, 10) + _encode(randomness, 16)
