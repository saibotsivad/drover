import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    privileged_image: str | None
    db_path: str
    socket_dir: str
    socket_host_dir: str
    docker_sock: str
    reaper_interval_seconds: int
    init_timeout_seconds: int
    log_level: str
    api_key_hash: str | None
    log_dir: str | None
    log_max_file_bytes: int


def load_config() -> Config:
    return Config(
        privileged_image=os.environ.get("PRIVILEGED_IMAGE"),
        db_path=os.environ.get("DB_PATH", "/var/lib/orchestrator/db.sqlite"),
        socket_dir=os.environ.get("SOCKET_DIR", "/var/run/microcontainers"),
        socket_host_dir=os.environ.get("SOCKET_HOST_DIR")
        or os.environ.get("SOCKET_DIR", "/var/run/microcontainers"),
        docker_sock=os.environ.get("DOCKER_SOCK", "/var/run/docker.sock"),
        reaper_interval_seconds=int(
            os.environ.get("REAPER_INTERVAL_SECONDS", "5")
        ),
        init_timeout_seconds=int(
            os.environ.get("DROVER_INIT_TIMEOUT_SECONDS", "20")
        ),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        api_key_hash=os.environ.get("DROVER_API_KEY"),
        log_dir=os.environ.get("DROVER_LOG_DIR"),
        log_max_file_bytes=int(
            os.environ.get("DROVER_LOG_MAX_FILE_BYTES", str(10 * 1024 * 1024))
        ),
    )
