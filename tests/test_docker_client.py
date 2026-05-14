"""Tests for the DockerClient streaming helper.

The non-streaming methods are exercised end-to-end against a real Docker
daemon by the e2e suite; this file is focused on stream_container_logs
because the rest of the system (LogCaptureManager) treats its output as
the trust boundary.
"""

import pytest
import httpx

from orchestrator.config import Config
from orchestrator.docker_client import (
    ContainerNotFoundError,
    DockerClient,
)


def _make_client(handler) -> DockerClient:
    config = Config(
        privileged_image=None,
        db_path="/tmp/x.db",
        socket_dir="/tmp/s",
        docker_sock="/dev/null",
        reaper_interval_seconds=5,
        init_timeout_seconds=20,
        log_level="DEBUG",
        api_key_hash=None,
        log_dir=None,
        log_max_file_bytes=10 * 1024 * 1024,
    )
    client = DockerClient(config)
    # Swap the underlying transport for a MockTransport so we don't need a
    # real Unix socket on the test runner.
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://docker/v1.44",
        timeout=httpx.Timeout(30.0, connect=5.0),
    )
    return client


@pytest.mark.asyncio
async def test_stream_container_logs_yields_chunks():
    captured_params: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params["url"] = str(request.url)
        body = b"chunk-1" + b"chunk-2"
        return httpx.Response(200, content=body)

    client = _make_client(handler)
    out = b""
    async for chunk in client.stream_container_logs(
        "abc", since=1700000000, follow=True, tail=10
    ):
        out += chunk

    assert b"chunk-1" in out and b"chunk-2" in out
    # Query string carries all four required Docker params.
    url = captured_params["url"]
    assert "stdout=true" in url
    assert "stderr=true" in url
    assert "follow=true" in url
    assert "timestamps=true" in url
    assert "since=1700000000" in url
    assert "tail=10" in url

    await client.close()


@pytest.mark.asyncio
async def test_stream_container_logs_without_since_or_tail():
    captured_url: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_url["url"] = str(request.url)
        return httpx.Response(200, content=b"")

    client = _make_client(handler)
    async for _ in client.stream_container_logs("abc"):
        pass

    url = captured_url["url"]
    assert "since=" not in url
    assert "tail=" not in url
    assert "follow=true" in url

    await client.close()


@pytest.mark.asyncio
async def test_stream_container_logs_404_raises_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "No such container"})

    client = _make_client(handler)
    with pytest.raises(ContainerNotFoundError):
        async for _ in client.stream_container_logs("missing"):
            pass

    await client.close()
