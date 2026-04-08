from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from orchestrator.config import Config, load_config
from orchestrator.database import Database


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    db = Database(config.db_path)
    await db.connect()

    app.state.config = config
    app.state.db = db

    # Placeholder - Background tasks (reaper, etc.) will be started here in later phases.

    yield

    await db.close()


app = FastAPI(title="Drover Orchestrator", lifespan=lifespan)


@app.get("/health")
async def health(request: Request):
    config: Config = request.app.state.config
    return {
        "healthy": True,
        "privileged_image": config.privileged_image,
    }
