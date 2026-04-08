import json
from urllib.parse import quote

import httpx

from orchestrator.config import Config


class DockerError(Exception):
    """Base exception for Docker API errors."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Docker API error ({status_code}): {message}")


class ImageNotFoundError(DockerError):
    """Raised when a Docker image is not found."""


class ContainerNotFoundError(DockerError):
    """Raised when a Docker container is not found."""


class ContainerConflictError(DockerError):
    """Raised on container state conflicts (e.g. already started/stopped)."""


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
            base_url="http://docker/v1.43",
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
                raise ContainerNotFoundError(resp.status_code, msg)
            raise DockerError(resp.status_code, msg)
        if resp.status_code == 409:
            raise ContainerConflictError(resp.status_code, msg)
        raise DockerError(resp.status_code, msg)

    # --- Images ---

    async def list_images(self, prefix: str = "drover/*") -> list[dict]:
        resp = await self._client.get(
            "/images/json",
            params={"filters": json.dumps({"reference": [prefix]})},
        )
        self._check(resp, entity="image")
        return resp.json()

    async def inspect_image(self, name: str) -> dict:
        resp = await self._client.get(
            f"/images/{quote(name, safe='')}/json",
        )
        self._check(resp, entity="image")
        return resp.json()

    # --- Containers ---

    async def create_container(self, config: dict) -> dict:
        resp = await self._client.post("/containers/create", json=config)
        self._check(resp, entity="container")
        return resp.json()

    async def start_container(self, container_id: str) -> None:
        resp = await self._client.post(
            f"/containers/{container_id}/start",
        )
        if resp.status_code == 304:
            return  # already started
        self._check(resp, entity="container")

    async def stop_container(
        self, container_id: str, timeout: int = 10
    ) -> None:
        resp = await self._client.post(
            f"/containers/{container_id}/stop",
            params={"t": timeout},
        )
        if resp.status_code == 304:
            return  # already stopped
        self._check(resp, entity="container")

    async def remove_container(
        self, container_id: str, *, force: bool = False
    ) -> None:
        resp = await self._client.delete(
            f"/containers/{container_id}",
            params={"force": str(force).lower(), "v": "true"},
        )
        self._check(resp, entity="container")

    async def inspect_container(self, container_id: str) -> dict:
        resp = await self._client.get(
            f"/containers/{container_id}/json",
        )
        self._check(resp, entity="container")
        return resp.json()

    async def get_container_logs(
        self, container_id: str, tail: str = "all"
    ) -> str:
        resp = await self._client.get(
            f"/containers/{container_id}/logs",
            params={"stdout": "true", "stderr": "true", "tail": tail},
        )
        self._check(resp, entity="container")
        return resp.text
