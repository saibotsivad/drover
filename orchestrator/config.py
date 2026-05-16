import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    privileged_image: str | None
    data_dir: str
    socket_dir: str
    socket_host_dir: str
    docker_sock: str
    reaper_interval_seconds: int
    init_timeout_seconds: int
    log_level: str
    api_key_hash: str | None
    log_dir: str | None
    log_max_file_bytes: int

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "db.sqlite")


def load_config() -> Config:
    return Config(
        privileged_image=os.environ.get("DROVER_PRIVILEGED_IMAGE"),
        data_dir=os.environ.get("DROVER_DATA_DIR", "/var/lib/orchestrator"),
        socket_dir="/var/run/microcontainers",
        socket_host_dir=os.environ.get(
            "DROVER_SOCKET_DIR", "/var/run/drover/sockets"
        ),
        docker_sock=os.environ.get("DROVER_DOCKER_SOCK", "/var/run/docker.sock"),
        reaper_interval_seconds=int(
            os.environ.get("DROVER_REAPER_INTERVAL_SECONDS", "5")
        ),
        init_timeout_seconds=int(
            os.environ.get("DROVER_INIT_TIMEOUT_SECONDS", "20")
        ),
        log_level=os.environ.get("DROVER_LOG_LEVEL", "INFO").upper(),
        api_key_hash=os.environ.get("DROVER_API_KEY"),
        log_dir=os.environ.get("DROVER_LOG_DIR"),
        log_max_file_bytes=int(
            os.environ.get("DROVER_LOG_MAX_FILE_BYTES", str(10 * 1024 * 1024))
        ),
    )
