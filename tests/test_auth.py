import hashlib
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from orchestrator.auth import auth_middleware, hash_api_key


# ---------------------------------------------------------------------------
# hash_api_key
# ---------------------------------------------------------------------------


def test_hash_api_key_returns_sha256_hex():
    key = "my-secret"
    expected = hashlib.sha256(key.encode()).hexdigest()
    assert hash_api_key(key) == expected


def test_hash_api_key_deterministic():
    assert hash_api_key("abc") == hash_api_key("abc")


def test_hash_api_key_different_inputs():
    assert hash_api_key("key-a") != hash_api_key("key-b")


# ---------------------------------------------------------------------------
# Helpers for middleware tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeURL:
    path: str


@dataclass
class _FakeState:
    config: object


class _FakeApp:
    def __init__(self, api_key_hash):
        self.state = _FakeState(
            config=type("C", (), {"api_key_hash": api_key_hash})()
        )


class _FakeRequest:
    def __init__(self, path, headers=None, api_key_hash=None):
        self.url = _FakeURL(path)
        self.headers = headers or {}
        self.app = _FakeApp(api_key_hash)


def _ok_response():
    """Simulate a successful downstream response."""
    resp = type("R", (), {"status_code": 200})()
    return resp


# ---------------------------------------------------------------------------
# Middleware tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_disabled_passes_through():
    """When api_key_hash is None, all requests pass through."""
    request = _FakeRequest("/containers", api_key_hash=None)
    call_next = AsyncMock(return_value=_ok_response())
    response = await auth_middleware(request, call_next)
    call_next.assert_awaited_once_with(request)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_always_public():
    """GET /health passes even with auth enabled and no token."""
    api_key_hash = hash_api_key("secret")
    request = _FakeRequest("/health", api_key_hash=api_key_hash)
    call_next = AsyncMock(return_value=_ok_response())
    response = await auth_middleware(request, call_next)
    call_next.assert_awaited_once_with(request)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_missing_auth_header_returns_401():
    api_key_hash = hash_api_key("secret")
    request = _FakeRequest("/containers", api_key_hash=api_key_hash)
    call_next = AsyncMock(return_value=_ok_response())
    response = await auth_middleware(request, call_next)
    assert response.status_code == 401
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_auth_header_returns_401():
    api_key_hash = hash_api_key("secret")
    request = _FakeRequest(
        "/containers",
        headers={"authorization": "Basic abc123"},
        api_key_hash=api_key_hash,
    )
    call_next = AsyncMock(return_value=_ok_response())
    response = await auth_middleware(request, call_next)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_wrong_key_returns_401():
    api_key_hash = hash_api_key("correct-key")
    request = _FakeRequest(
        "/containers",
        headers={"authorization": "Bearer wrong-key"},
        api_key_hash=api_key_hash,
    )
    call_next = AsyncMock(return_value=_ok_response())
    response = await auth_middleware(request, call_next)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_correct_key_passes():
    api_key_hash = hash_api_key("correct-key")
    request = _FakeRequest(
        "/containers",
        headers={"authorization": "Bearer correct-key"},
        api_key_hash=api_key_hash,
    )
    call_next = AsyncMock(return_value=_ok_response())
    response = await auth_middleware(request, call_next)
    call_next.assert_awaited_once_with(request)
    assert response.status_code == 200
