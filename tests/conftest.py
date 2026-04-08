import os
import tempfile

import pytest

from orchestrator.config import Config
from orchestrator.database import Database


@pytest.fixture
def config(tmp_path):
    return Config(
        privileged_image=None,
        db_path=str(tmp_path / "test.db"),
        socket_dir=str(tmp_path / "sockets"),
        docker_sock="/dev/null",
        reaper_interval_seconds=5,
        log_level="DEBUG",
    )


@pytest.fixture
async def db(config):
    database = Database(config.db_path)
    await database.connect()
    yield database
    await database.close()
