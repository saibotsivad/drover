import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    privileged_image: str | None
    db_path: str
    socket_dir: str
    docker_sock: str
    reaper_interval_seconds: int
    log_level: str


def load_config() -> Config:
    return Config(
        privileged_image=os.environ.get("PRIVILEGED_IMAGE"),
        db_path=os.environ.get("DB_PATH", "/var/lib/orchestrator/db.sqlite"),
        socket_dir=os.environ.get("SOCKET_DIR", "/var/run/microcontainers"),
        docker_sock=os.environ.get("DOCKER_SOCK", "/var/run/docker.sock"),
        reaper_interval_seconds=int(
            os.environ.get("REAPER_INTERVAL_SECONDS", "5")
        ),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
