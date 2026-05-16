from orchestrator.config import Config, load_config


def test_load_config_defaults(monkeypatch):
    monkeypatch.delenv("DROVER_PRIVILEGED_IMAGE", raising=False)
    monkeypatch.delenv("DROVER_DATA_DIR", raising=False)
    monkeypatch.delenv("DROVER_SOCKET_DIR", raising=False)
    monkeypatch.delenv("DROVER_DOCKER_SOCK", raising=False)
    monkeypatch.delenv("DROVER_REAPER_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("DROVER_INIT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("DROVER_LOG_LEVEL", raising=False)
    monkeypatch.delenv("DROVER_API_KEY", raising=False)
    monkeypatch.delenv("DROVER_LOG_DIR", raising=False)
    monkeypatch.delenv("DROVER_LOG_MAX_FILE_BYTES", raising=False)

    config = load_config()

    assert config.privileged_image is None
    assert config.data_dir == "/var/lib/orchestrator"
    assert config.db_path == "/var/lib/orchestrator/db.sqlite"
    assert config.socket_dir == "/var/run/microcontainers"
    assert config.socket_host_dir == "/var/run/drover/sockets"
    assert config.docker_sock == "/var/run/docker.sock"
    assert config.reaper_interval_seconds == 5
    assert config.init_timeout_seconds == 20
    assert config.log_level == "INFO"
    assert config.api_key_hash is None
    assert config.log_dir is None
    assert config.log_max_file_bytes == 10 * 1024 * 1024


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("DROVER_PRIVILEGED_IMAGE", "my-priv-image")
    monkeypatch.setenv("DROVER_DATA_DIR", "/tmp/drover-data")
    monkeypatch.setenv("DROVER_SOCKET_DIR", "/tmp/host-socks")
    monkeypatch.setenv("DROVER_DOCKER_SOCK", "/tmp/docker.sock")
    monkeypatch.setenv("DROVER_REAPER_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("DROVER_INIT_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("DROVER_LOG_LEVEL", "debug")
    monkeypatch.setenv("DROVER_API_KEY", "abc123hash")
    monkeypatch.setenv("DROVER_LOG_DIR", "/var/lib/orchestrator/logs")
    monkeypatch.setenv("DROVER_LOG_MAX_FILE_BYTES", "2048")

    config = load_config()

    assert config.privileged_image == "my-priv-image"
    assert config.data_dir == "/tmp/drover-data"
    assert config.db_path == "/tmp/drover-data/db.sqlite"
    assert config.socket_host_dir == "/tmp/host-socks"
    assert config.docker_sock == "/tmp/docker.sock"
    assert config.reaper_interval_seconds == 30
    assert config.init_timeout_seconds == 45
    assert config.log_level == "DEBUG"
    assert config.api_key_hash == "abc123hash"
    assert config.log_dir == "/var/lib/orchestrator/logs"
    assert config.log_max_file_bytes == 2048


def test_config_is_frozen():
    config = Config(
        privileged_image=None,
        data_dir="/tmp",
        socket_dir="/tmp/s",
        socket_host_dir="/tmp/sh",
        docker_sock="/tmp/d",
        reaper_interval_seconds=5,
        init_timeout_seconds=20,
        log_level="INFO",
        api_key_hash=None,
        log_dir=None,
        log_max_file_bytes=10 * 1024 * 1024,
    )
    try:
        config.data_dir = "/other"  # type: ignore
        assert False, "Expected FrozenInstanceError"
    except AttributeError:
        pass
