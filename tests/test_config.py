from orchestrator.config import (
    Config,
    DB_PATH,
    DOCKER_SOCK,
    LOG_DIR,
    SOCKET_DIR,
    load_config,
)


def _clear_env(monkeypatch):
    """Strip every Drover-relevant env var so load_config sees a clean slate."""
    for name in (
        "PRIVILEGED_IMAGE",
        "REAPER_INTERVAL_SECONDS",
        "DROVER_INIT_TIMEOUT_SECONDS",
        "LOG_LEVEL",
        "DROVER_API_KEY",
        "DROVER_ENABLE_WORKER_LOGS",
        "DROVER_LOG_MAX_FILE_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)


def test_load_config_defaults(monkeypatch):
    _clear_env(monkeypatch)
    config = load_config()

    assert config.privileged_image is None
    assert config.db_path == "/var/lib/drover/data/db.sqlite"
    assert config.socket_dir == "/var/run/drover/sockets"
    assert config.docker_sock == "/var/run/docker.sock"
    assert config.reaper_interval_seconds == 5
    assert config.init_timeout_seconds == 20
    assert config.log_level == "INFO"
    assert config.api_key_hash is None
    assert config.log_dir is None
    assert config.log_max_file_bytes == 10 * 1024 * 1024


def test_paths_are_fixed_constants(monkeypatch):
    """Path defaults are module-level constants, not env-driven."""
    assert DB_PATH == "/var/lib/drover/data/db.sqlite"
    assert SOCKET_DIR == "/var/run/drover/sockets"
    assert DOCKER_SOCK == "/var/run/docker.sock"
    assert LOG_DIR == "/var/lib/drover/logs"


def test_path_env_vars_are_ignored(monkeypatch):
    """No DROVER_DB_PATH / DROVER_SOCKET_DIR / DROVER_DOCKER_SOCK escape hatch."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DROVER_DB_PATH", "/tmp/test.db")
    monkeypatch.setenv("DROVER_SOCKET_DIR", "/tmp/socks")
    monkeypatch.setenv("DROVER_DOCKER_SOCK", "/tmp/docker.sock")

    config = load_config()

    assert config.db_path == DB_PATH
    assert config.socket_dir == SOCKET_DIR
    assert config.docker_sock == DOCKER_SOCK


def test_load_config_tunables_from_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("PRIVILEGED_IMAGE", "my-priv-image")
    monkeypatch.setenv("REAPER_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("DROVER_INIT_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("DROVER_API_KEY", "abc123hash")
    monkeypatch.setenv("DROVER_ENABLE_WORKER_LOGS", "true")
    monkeypatch.setenv("DROVER_LOG_MAX_FILE_BYTES", "2048")

    config = load_config()

    assert config.privileged_image == "my-priv-image"
    assert config.reaper_interval_seconds == 30
    assert config.init_timeout_seconds == 45
    assert config.log_level == "DEBUG"
    assert config.api_key_hash == "abc123hash"
    assert config.log_dir == "/var/lib/drover/logs"
    assert config.log_max_file_bytes == 2048


def test_enable_container_logs_only_true_string_enables(monkeypatch):
    """Only the exact string "true" turns capture on."""
    _clear_env(monkeypatch)
    for value in ("1", "TRUE", "True", "yes", "on", ""):
        monkeypatch.setenv("DROVER_ENABLE_WORKER_LOGS", value)
        assert load_config().log_dir is None, f"value {value!r} should not enable"

    monkeypatch.setenv("DROVER_ENABLE_WORKER_LOGS", "true")
    assert load_config().log_dir == "/var/lib/drover/logs"


def test_config_is_frozen():
    config = Config(
        privileged_image=None,
        db_path="/tmp/db",
        socket_dir="/tmp/s",
        docker_sock="/tmp/d",
        reaper_interval_seconds=5,
        init_timeout_seconds=20,
        log_level="INFO",
        api_key_hash=None,
        log_dir=None,
        log_max_file_bytes=10 * 1024 * 1024,
    )
    try:
        config.db_path = "/other"  # type: ignore
        assert False, "Expected FrozenInstanceError"
    except AttributeError:
        pass
