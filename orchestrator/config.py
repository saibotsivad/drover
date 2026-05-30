import os
from dataclasses import dataclass


# Fixed in-container paths.  These are NOT overridable from the
# environment — the operator binds host paths to these container-internal
# locations and the orchestrator never touches anything else.
DB_PATH = "/var/lib/drover/data/db.sqlite"
LOG_DIR = "/var/lib/drover/logs"
# Each micro-container gets its own per-container folder under SOCKET_DIR
# (``{socket_dir}/{container_id}/``).  The whole folder is bind-mounted
# into the micro-container at the same path, and the orchestrator's
# control socket lives in it as ORCHESTRATOR_SOCKET_NAME.  Using a folder
# (rather than a single socket file) lets us add more sockets per
# container later — e.g. one per interactive shell.
SOCKET_DIR = "/var/run/drover/sockets"
ORCHESTRATOR_SOCKET_NAME = "orchestrator.sock"
DOCKER_SOCK = "/var/run/docker.sock"


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
        db_path=DB_PATH,
        socket_dir=SOCKET_DIR,
        docker_sock=DOCKER_SOCK,
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
