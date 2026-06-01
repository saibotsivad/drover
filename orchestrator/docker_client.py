import json
import logging
from typing import AsyncIterator
from urllib.parse import quote

import httpx

from orchestrator.config import Config
from orchestrator.errors import (
    WorkerConflictError,
    WorkerNotFoundError,
    DockerError,
    ImageNotFoundError,
)

logger = logging.getLogger(__name__)


def _parse_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return body.get("message", resp.text)
    except Exception:
        return resp.text


class DockerClient:
    """Thin async wrapper around the Docker Engine API over a Unix socket."""

    def __init__(self, config: Config) -> None:
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=config.docker_sock),
            base_url="http://docker/v1.44",
            timeout=httpx.Timeout(30.0, connect=5.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _check(
        self, resp: httpx.Response, *, entity: str = "resource"
    ) -> None:
        if resp.status_code < 400:
            return
        msg = _parse_error(resp)
        if resp.status_code == 404:
            if entity == "image":
                raise ImageNotFoundError(resp.status_code, msg)
            if entity == "container":
                raise WorkerNotFoundError(resp.status_code, msg)
            raise DockerError(resp.status_code, msg)
        if resp.status_code == 409:
            raise WorkerConflictError(resp.status_code, msg)
        raise DockerError(resp.status_code, msg)

    # --- Images ---

    async def get_info(self) -> dict:
        """Return the Docker daemon system info (GET /info)."""
        resp = await self._client.get("/info")
        self._check(resp)
        return resp.json()

    async def list_images(self, name: str | None = None) -> list[dict]:
        """List Drover-managed images.

        Always filters on ``drover.managed=true``.  When ``name`` is given,
        also filters on ``drover.name=<name>`` so callers can look up a
        single image by its short name without fetching the whole set.
        """
        labels = ["drover.managed=true"]
        if name is not None:
            labels.append(f"drover.name={name}")
        logger.debug("GET /images/json labels=%s", labels)
        resp = await self._client.get(
            "/images/json",
            params={"filters": json.dumps({"label": labels})},
        )
        logger.debug("GET /images/json -> %s", resp.status_code)
        self._check(resp, entity="image")
        return resp.json()

    async def inspect_image(self, name: str) -> dict:
        logger.debug("GET /images/%s/json", name)
        resp = await self._client.get(
            f"/images/{quote(name, safe='')}/json",
        )
        logger.debug("GET /images/%s/json -> %s", name, resp.status_code)
        self._check(resp, entity="image")
        return resp.json()

    # --- Containers ---

    async def create_worker(self, config: dict) -> dict:
        logger.debug("POST /containers/create")
        resp = await self._client.post("/containers/create", json=config)
        logger.debug("POST /containers/create -> %s", resp.status_code)
        self._check(resp, entity="container")
        return resp.json()

    async def start_worker(self, worker_id: str) -> None:
        logger.debug("POST /containers/%s/start", worker_id)
        resp = await self._client.post(
            f"/containers/{worker_id}/start",
        )
        logger.debug("POST /containers/%s/start -> %s", worker_id, resp.status_code)
        if resp.status_code == 304:
            return  # already started
        self._check(resp, entity="container")

    async def stop_worker(
        self, worker_id: str, timeout: int = 10
    ) -> None:
        logger.debug("POST /containers/%s/stop", worker_id)
        resp = await self._client.post(
            f"/containers/{worker_id}/stop",
            params={"t": timeout},
        )
        logger.debug("POST /containers/%s/stop -> %s", worker_id, resp.status_code)
        if resp.status_code == 304:
            return  # already stopped
        self._check(resp, entity="container")

    async def remove_worker(
        self, worker_id: str, *, force: bool = False
    ) -> None:
        logger.debug("DELETE /containers/%s force=%s", worker_id, force)
        resp = await self._client.delete(
            f"/containers/{worker_id}",
            params={"force": str(force).lower(), "v": "true"},
        )
        logger.debug("DELETE /containers/%s -> %s", worker_id, resp.status_code)
        self._check(resp, entity="container")

    async def inspect_worker(self, worker_id: str) -> dict:
        logger.debug("GET /containers/%s/json", worker_id)
        resp = await self._client.get(
            f"/containers/{worker_id}/json",
        )
        logger.debug("GET /containers/%s/json -> %s", worker_id, resp.status_code)
        self._check(resp, entity="container")
        return resp.json()

    async def get_worker_logs(
        self, worker_id: str, tail: str = "all"
    ) -> str:
        logger.debug("GET /containers/%s/logs tail=%s", worker_id, tail)
        resp = await self._client.get(
            f"/containers/{worker_id}/logs",
            params={"stdout": "true", "stderr": "true", "tail": tail},
        )
        logger.debug("GET /containers/%s/logs -> %s", worker_id, resp.status_code)
        self._check(resp, entity="container")
        return resp.text

    async def stream_worker_logs(
        self,
        worker_id: str,
        *,
        since: float | int | None = None,
        follow: bool = True,
        tail: int | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield raw bytes from Docker's multiplexed log stream.

        Caller is responsible for parsing the 8-byte-header frame format
        Docker uses when the container does not have a TTY.  We pass
        ``timestamps=1`` so each frame's payload is prefixed with an
        RFC3339Nano timestamp.
        """
        params: dict[str, str] = {
            "stdout": "true",
            "stderr": "true",
            "follow": "true" if follow else "false",
            "timestamps": "true",
        }
        if since is not None:
            params["since"] = str(since)
        if tail is not None:
            params["tail"] = str(tail)
        logger.debug(
            "GET /containers/%s/logs (stream) since=%s follow=%s tail=%s",
            worker_id,
            since,
            follow,
            tail,
        )
        async with self._client.stream(
            "GET",
            f"/containers/{worker_id}/logs",
            params=params,
            timeout=httpx.Timeout(None, connect=5.0),
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                self._check(resp, entity="container")
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk
