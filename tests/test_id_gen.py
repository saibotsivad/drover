import time

from orchestrator.id_gen import _ALPHABET, _encode, generate_id


def test_id_length():
    assert len(generate_id()) == 26


def test_id_uses_crockford_base32():
    id_ = generate_id()
    for ch in id_:
        assert ch in _ALPHABET, f"unexpected character {ch!r}"


def test_ids_are_unique():
    ids = {generate_id() for _ in range(100)}
    assert len(ids) == 100


def test_ids_are_sortable():
    """IDs generated later should sort after earlier ones (timestamp prefix)."""
    a = generate_id()
    # Tiny sleep to ensure timestamp advances at least 1 ms
    time.sleep(0.002)
    b = generate_id()
    assert b > a


def test_encode_zero():
    assert _encode(0, 4) == "0000"


def test_encode_max():
    # 5 bits per char, 4 chars = 20 bits max
    result = _encode(0xFFFFF, 4)
    assert len(result) == 4
    for ch in result:
        assert ch in _ALPHABET
