import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from orchestrator.auth import auth_middleware
from orchestrator.config import Config, load_config
from orchestrator.container_manager import ContainerManager
from orchestrator.database import Database
from orchestrator.docker_client import DockerClient
from orchestrator.errors import (
    ContainerNotFound,
    ContainerStateConflict,
    DockerError,
)
from orchestrator.log_capture import LogCaptureManager
from orchestrator.routers import containers, images
from orchestrator.socket_manager import SocketManager


class _JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.") + f"{record.msecs:03.0f}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    # Suppress noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


def _detect_own_container_id() -> str | None:
    """Return this process's Docker container ID by inspecting cgroup membership.

    Works for both cgroupv1 and cgroupv2 in standard Docker deployments.
    Returns ``None`` if detection fails (non-Docker runtime or unusual setup).
    """
    try:
        text = Path("/proc/self/cgroup").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        m = re.search(r"/([a-f0-9]{64})(?:\.scope)?$", line)
        if m:
            return m.group(1)
    return None


async def _reaper_loop(
    config: Config, db: Database, container_manager: ContainerManager
) -> None:
    """Background task: stop containers that have exceeded their idle timeout."""
    while True:
        await asyncio.sleep(config.reaper_interval_seconds)
        try:
            rows = await db.fetchall(
                "SELECT id, last_seen, timeout_seconds FROM containers "
                "WHERE status = 'running' AND last_seen IS NOT NULL AND timeout_seconds > 0",
            )
            now = datetime.now(timezone.utc)
            for row in rows:
                last_seen = datetime.fromisoformat(row["last_seen"])
                elapsed = (now - last_seen).total_seconds()
                if elapsed > row["timeout_seconds"]:
                    logger.info(
                        "Container %s timed out (last_seen=%s, elapsed=%.1fs, timeout=%ds), stopping",
                        row["id"],
                        row["last_seen"],
                        elapsed,
                        row["timeout_seconds"],
                    )
                    try:
                        await container_manager.stop_container(row["id"])
                    except ContainerStateConflict:
                        # State changed between query and stop (e.g. already stopping)
                        logger.debug(
                            "Container %s state changed before reaper could stop it",
                            row["id"],
                        )
                    except ContainerNotFound:
                        logger.debug(
                            "Container %s not found when reaper tried to stop it",
                            row["id"],
                        )
                    except Exception:
                        logger.exception(
                            "Reaper failed to stop container %s", row["id"]
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reaper loop encountered an error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    setup_logging(config.log_level)
    logger.info("Starting Drover orchestrator")
    if config.api_key_hash is None:
        logger.warning(
            "DROVER_API_KEY is not set — API authentication is disabled"
        )
    db = Database(config.db_path)
    await db.connect()
    docker = DockerClient(config)
    sockets = SocketManager(config, db)
    log_capture = LogCaptureManager(config, docker)

    container_manager = ContainerManager(config, db, docker, sockets, log_capture)
    await container_manager.sync_containers()

    async def _handle_container_done(container_id: str) -> None:
        try:
            await container_manager.stop_container(container_id)
            logger.info("Container %s stopped via done signal", container_id)
        except (ContainerStateConflict, ContainerNotFound):
            pass
        except Exception:
            logger.exception(
                "Failed to stop container %s after done signal", container_id
            )

    sockets.set_done_callback(_handle_container_done)
    sockets.set_ready_callback(container_manager.on_container_ready)

    own_id = _detect_own_container_id()
    if own_id is None:
        logger.warning(
            "Could not detect orchestrator's own Docker container ID from "
            "/proc/self/cgroup; orchestrator log endpoint will return 503"
        )
    else:
        logger.info("Detected orchestrator container ID: %s", own_id)

    app.state.config = config
    app.state.db = db
    app.state.docker = docker
    app.state.sockets = sockets
    app.state.log_capture = log_capture
    app.state.container_manager = container_manager
    app.state.orchestrator_docker_id = own_id

    reaper_task = asyncio.create_task(
        _reaper_loop(config, db, container_manager)
    )

    yield

    reaper_task.cancel()
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass

    await container_manager.shutdown()
    await log_capture.shutdown()
    await sockets.close_all()
    await docker.close()
    await db.close()


app = FastAPI(title="Drover Orchestrator", lifespan=lifespan)
app.include_router(images.router)
app.include_router(containers.router)


@app.exception_handler(httpx.RequestError)
async def _httpx_request_error(request: Request, exc: httpx.RequestError) -> JSONResponse:
    msg = str(exc)
    config: Config = request.app.state.config
    sock_path = config.docker_sock
    logger.error("Docker daemon unreachable (%s): %s", sock_path, msg)

    sock_hint = _docker_sock_hint(sock_path)
    if sock_hint is not None:
        detail = sock_hint
    elif "Permission denied" in msg:
        detail = (
            f"Permission denied accessing the Docker socket at {sock_path}. "
            "The orchestrator user is not in the socket's group — the entrypoint "
            "tries to join it automatically, so this usually means the socket "
            "wasn't a socket when the container started."
        )
    else:
        detail = f"Cannot reach the Docker daemon at {sock_path}: {msg}"
    return JSONResponse(status_code=503, content={"detail": detail})


def _docker_sock_hint(sock_path: str) -> str | None:
    """Return a human-readable hint when *sock_path* isn't a usable socket."""
    import os
    import stat

    try:
        st = os.stat(sock_path)
    except FileNotFoundError:
        return (
            f"The Docker socket at {sock_path} does not exist inside the "
            "container. Check that the bind-mount is configured."
        )
    except OSError as exc:
        return f"Cannot stat {sock_path}: {exc}"

    if stat.S_ISSOCK(st.st_mode):
        return None
    if stat.S_ISDIR(st.st_mode):
        return (
            f"{sock_path} is a directory inside the container, not a socket. "
            "Docker created it because the bind-mount source path does not "
            "exist on the host. For rootless Docker the socket lives at "
            "/run/user/$UID/docker.sock — set DROVER_DOCKER_SOCK on the host "
            "(e.g. DROVER_DOCKER_SOCK=$XDG_RUNTIME_DIR/docker.sock) and "
            "recreate the stack."
        )
    return f"{sock_path} exists but is not a Unix socket (mode={oct(st.st_mode)})."


@app.exception_handler(DockerError)
async def _docker_error(request: Request, exc: DockerError) -> JSONResponse:
    logger.warning("Docker API error: %s", exc)
    return JSONResponse(
        status_code=502,
        content={"detail": f"Docker API error ({exc.status_code}): {exc.message}"},
    )


@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:
    method = request.method
    path = request.url.path
    logger.info("%s %s", method, path)
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info("%s %s -> %d (%.0fms)", method, path, response.status_code, duration_ms)
    return response


app.middleware("http")(auth_middleware)


@app.get("/health")
async def health(request: Request):
    config: Config = request.app.state.config
    return {
        "healthy": True,
        "privileged_image": config.privileged_image,
    }
