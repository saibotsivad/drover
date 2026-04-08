import os

from orchestrator.config import Config, load_config


def test_load_config_defaults(monkeypatch):
    monkeypatch.delenv("PRIVILEGED_IMAGE", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("SOCKET_DIR", raising=False)
    monkeypatch.delenv("DOCKER_SOCK", raising=False)
    monkeypatch.delenv("REAPER_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    config = load_config()

    assert config.privileged_image is None
    assert config.db_path == "/var/lib/orchestrator/db.sqlite"
    assert config.socket_dir == "/var/run/microcontainers"
    assert config.docker_sock == "/var/run/docker.sock"
    assert config.reaper_interval_seconds == 5
    assert config.log_level == "INFO"


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("PRIVILEGED_IMAGE", "my-priv-image")
    monkeypatch.setenv("DB_PATH", "/tmp/test.db")
    monkeypatch.setenv("SOCKET_DIR", "/tmp/socks")
    monkeypatch.setenv("DOCKER_SOCK", "/tmp/docker.sock")
    monkeypatch.setenv("REAPER_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    config = load_config()

    assert config.privileged_image == "my-priv-image"
    assert config.db_path == "/tmp/test.db"
    assert config.socket_dir == "/tmp/socks"
    assert config.docker_sock == "/tmp/docker.sock"
    assert config.reaper_interval_seconds == 30
    assert config.log_level == "DEBUG"


def test_config_is_frozen():
    config = Config(
        privileged_image=None,
        db_path="/tmp/db",
        socket_dir="/tmp/s",
        docker_sock="/tmp/d",
        reaper_interval_seconds=5,
        log_level="INFO",
    )
    try:
        config.db_path = "/other"  # type: ignore
        assert False, "Expected FrozenInstanceError"
    except AttributeError:
        pass
