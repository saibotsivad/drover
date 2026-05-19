import os
from dataclasses import dataclass


# Fixed in-container paths.  See README.md "Container paths and host
# bindings" for the binding contract — the operator binds host paths to
# these container-internal locations; the orchestrator itself never sees
# the host paths.
DATA_DIR = "/var/lib/drover/data"
LOG_DIR = "/var/lib/drover/logs"
SOCKET_DIR = "/var/run/drover/sockets"
DOCKER_SOCK = "/var/run/docker.sock"
DB_PATH = f"{DATA_DIR}/db.sqlite"


@dataclass(frozen=True)
class Config:
    privileged_image: str | None
    db_path: str
    socket_dir: str
    docker_sock: str
    reaper_interval_seconds: int
    init_timeout_seconds: int
    log_level: str
    api_key_hash: str | None
    log_dir: str | None
    log_max_file_bytes: int


def load_config() -> Config:
    enable_container_logs = (
        os.environ.get("DROVER_ENABLE_CONTAINER_LOGS") == "true"
    )
    return Config(
        privileged_image=os.environ.get("PRIVILEGED_IMAGE"),
        db_path=os.environ.get("DROVER_DB_PATH", DB_PATH),
        socket_dir=os.environ.get("DROVER_SOCKET_DIR", SOCKET_DIR),
        docker_sock=os.environ.get("DROVER_DOCKER_SOCK", DOCKER_SOCK),
        reaper_interval_seconds=int(
            os.environ.get("REAPER_INTERVAL_SECONDS", "5")
        ),
        init_timeout_seconds=int(
            os.environ.get("DROVER_INIT_TIMEOUT_SECONDS", "20")
        ),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        api_key_hash=os.environ.get("DROVER_API_KEY"),
        log_dir=LOG_DIR if enable_container_logs else None,
        log_max_file_bytes=int(
            os.environ.get("DROVER_LOG_MAX_FILE_BYTES", str(10 * 1024 * 1024))
        ),
    )
