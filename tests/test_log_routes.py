"""Tests for the ``/containers/{id}/logs[/files[/<name>]]`` REST routes.

Wires a real ``LogCaptureManager`` (on a ``tmp_path`` directory) into a
minimal FastAPI app along with a fake container_manager so we can hit
the 404/409/200 paths without spinning up Docker.  Lifecycle and capture
mechanics are covered in test_log_capture.py and test_container_manager.py.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from orchestrator.config import Config
from orchestrator.container_manager import ContainerNotFound
from orchestrator.log_capture import LogCaptureManager
from orchestrator.routers import containers as containers_router


def _config(tmp_path, log_dir=None) -> Config:
    return Config(
        privileged_image=None,
        data_dir=str(tmp_path),
        socket_dir=str(tmp_path / "s"),
        socket_host_dir=str(tmp_path / "s"),
        docker_sock="/dev/null",
        reaper_interval_seconds=5,
        init_timeout_seconds=20,
        log_level="DEBUG",
        api_key_hash=None,
        log_dir=str(log_dir) if log_dir else None,
        log_max_file_bytes=10 * 1024 * 1024,
    )


def _build_app(
    config: Config,
    log_capture: LogCaptureManager,
    *,
    container_exists: bool = True,
    docker_id: str | None = "docker_xyz",
    orchestrator_docker_id: str | None = "orch_dock_id",
    orchestrator_logs: str = "hello\nworld\n",
) -> FastAPI:
    app = FastAPI()
    app.include_router(containers_router.router)

    cm = MagicMock()

    async def fake_get(container_id):
        if not container_exists:
            raise ContainerNotFound(container_id)
        return MagicMock(id=container_id, status="running")

    cm.get_container = fake_get

    db = MagicMock()

    async def fetchone(query, args):
        if not container_exists:
            return None
        return {"docker_id": docker_id}

    db.fetchone = fetchone

    docker = MagicMock()

    async def get_logs(cid, tail="all"):
        if cid == orchestrator_docker_id:
            return orchestrator_logs
        return "hello\nworld\n"

    docker.get_container_logs = AsyncMock(side_effect=get_logs)

    app.state.container_manager = cm
    app.state.log_capture = log_capture
    app.state.db = db
    app.state.docker = docker
    app.state.orchestrator_docker_id = orchestrator_docker_id
    return app


@pytest.mark.asyncio
async def test_list_files_404_when_container_missing(tmp_path):
    config = _config(tmp_path, log_dir=tmp_path / "logs")
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture, container_exists=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/missing/logs/files")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_files_409_when_disabled(tmp_path):
    config = _config(tmp_path, log_dir=None)
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/cnt_abc/logs/files")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_list_files_empty_array(tmp_path):
    config = _config(tmp_path, log_dir=tmp_path / "logs")
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/cnt_abc/logs/files")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_files_returns_sorted_names(tmp_path):
    log_dir = tmp_path / "logs"
    (log_dir / "cnt_abc").mkdir(parents=True)
    for name in ["0.log", "2.log", "10.log", ".cursor"]:
        (log_dir / "cnt_abc" / name).write_text("x")
    config = _config(tmp_path, log_dir=log_dir)
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/cnt_abc/logs/files")
    assert r.status_code == 200
    assert r.json() == ["0.log", "2.log", "10.log"]


@pytest.mark.asyncio
async def test_get_file_200_for_existing(tmp_path):
    log_dir = tmp_path / "logs"
    (log_dir / "cnt_abc").mkdir(parents=True)
    (log_dir / "cnt_abc" / "0.log").write_text("hello world\n")
    config = _config(tmp_path, log_dir=log_dir)
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/cnt_abc/logs/files/0.log")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "hello world" in r.text


@pytest.mark.asyncio
async def test_get_file_404_for_bad_name(tmp_path):
    log_dir = tmp_path / "logs"
    (log_dir / "cnt_abc").mkdir(parents=True)
    (log_dir / "cnt_abc" / "0.log").write_text("ok")
    config = _config(tmp_path, log_dir=log_dir)
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        # FastAPI declines path traversal in the URL itself; check the
        # other invalid-name shapes that do reach the route handler.
        for bad in ["foo.log", "0.txt", "1.log"]:
            r = await ac.get(f"/containers/cnt_abc/logs/files/{bad}")
            assert r.status_code == 404, bad


@pytest.mark.asyncio
async def test_get_file_404_when_directory_missing(tmp_path):
    config = _config(tmp_path, log_dir=tmp_path / "logs")
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/cnt_abc/logs/files/0.log")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_file_409_when_disabled(tmp_path):
    config = _config(tmp_path, log_dir=None)
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/cnt_abc/logs/files/0.log")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_get_file_404_when_container_missing(tmp_path):
    config = _config(tmp_path, log_dir=tmp_path / "logs")
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture, container_exists=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/missing/logs/files/0.log")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_live_logs_proxies_to_docker(tmp_path):
    config = _config(tmp_path, log_dir=tmp_path / "logs")
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/cnt_abc/logs")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "hello" in r.text


@pytest.mark.asyncio
async def test_live_logs_404_when_container_missing(tmp_path):
    config = _config(tmp_path, log_dir=tmp_path / "logs")
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture, container_exists=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/missing/logs")
    assert r.status_code == 404


# --- /logs/orchestrator ---------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_logs_filters_by_container_id(tmp_path):
    config = _config(tmp_path, log_dir=tmp_path / "logs")
    log_capture = LogCaptureManager(config, docker=None)
    body = (
        "2026-05-14T00:00:00 starting up\n"
        "2026-05-14T00:00:01 container cnt_abc created\n"
        "2026-05-14T00:00:02 container cnt_other created\n"
        "2026-05-14T00:00:03 cnt_abc ready\n"
    )
    app = _build_app(config, log_capture, orchestrator_logs=body)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/cnt_abc/logs/orchestrator")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "cnt_abc created" in r.text
    assert "cnt_abc ready" in r.text
    assert "cnt_other" not in r.text
    assert "starting up" not in r.text


@pytest.mark.asyncio
async def test_orchestrator_logs_503_when_id_not_detected(tmp_path):
    config = _config(tmp_path, log_dir=tmp_path / "logs")
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture, orchestrator_docker_id=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/cnt_abc/logs/orchestrator")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_orchestrator_logs_404_when_container_missing(tmp_path):
    config = _config(tmp_path, log_dir=tmp_path / "logs")
    log_capture = LogCaptureManager(config, docker=None)
    app = _build_app(config, log_capture, container_exists=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/containers/missing/logs/orchestrator")
    assert r.status_code == 404
