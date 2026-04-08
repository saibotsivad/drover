import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.responses import Response

from orchestrator.config import Config, load_config
from orchestrator.container_manager import ContainerManager
from orchestrator.database import Database
from orchestrator.docker_client import DockerClient
from orchestrator.routers import containers, images

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    db = Database(config.db_path)
    await db.connect()
    docker = DockerClient(config)

    app.state.config = config
    app.state.db = db
    app.state.docker = docker
    app.state.container_manager = ContainerManager(config, db, docker)

    # Placeholder - Background tasks (reaper, etc.) will be started here in later phases.

    yield

    await docker.close()
    await db.close()


app = FastAPI(title="Drover Orchestrator", lifespan=lifespan)
app.include_router(images.router)
app.include_router(containers.router)


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


@app.get("/health")
async def health(request: Request):
    config: Config = request.app.state.config
    return {
        "healthy": True,
        "privileged_image": config.privileged_image,
    }
