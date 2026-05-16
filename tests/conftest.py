import pytest

from orchestrator.config import Config
from orchestrator.database import Database


@pytest.fixture
def config(tmp_path):
    return Config(
        privileged_image=None,
        data_dir=str(tmp_path),
        socket_dir=str(tmp_path / "sockets"),
        socket_host_dir=str(tmp_path / "sockets"),
        docker_sock="/dev/null",
        reaper_interval_seconds=5,
        init_timeout_seconds=20,
        log_level="DEBUG",
        api_key_hash=None,
        log_dir=None,
        log_max_file_bytes=10 * 1024 * 1024,
    )


@pytest.fixture
async def db(config):
    database = Database(config.db_path)
    await database.connect()
    yield database
    await database.close()
