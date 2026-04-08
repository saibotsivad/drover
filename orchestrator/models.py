from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# --- Container models ---


class ContainerStatus(str, Enum):
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    resuming = "resuming"
    destroying = "destroying"
    destroyed = "destroyed"


class CreateContainerRequest(BaseModel):
    image: str
    privileged: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    label: str | None = None
    timeout_seconds: int = Field(default=300, gt=0)


class ContainerResponse(BaseModel):
    id: str
    image: str
    privileged: bool
    status: ContainerStatus
    label: str | None = None
    timeout_seconds: int
    created_at: datetime
    stopped_at: datetime | None = None
    last_seen: datetime | None = None


# --- Exec models ---


class ExecRequest(BaseModel):
    command: str


class ExecResponse(BaseModel):
    command_id: str


class CommandStatus(str, Enum):
    pending = "pending"
    running = "running"
    complete = "complete"


class CommandMessage(BaseModel):
    seq: int
    stream: str
    data: str


class ExecStatusResponse(BaseModel):
    command_id: str
    status: CommandStatus
    exit_code: int | None = None
    messages: list[CommandMessage] = Field(default_factory=list)


# --- Image models ---


class ImageSummary(BaseModel):
    name: str
    tags: list[str]
    size: int
    created: datetime

    @classmethod
    def from_docker(cls, data: dict) -> "ImageSummary":
        repo_tags = data.get("RepoTags") or []
        # Extract short name from first tag (e.g. "drover/python-runner:latest" -> "python-runner")
        name = ""
        tags = []
        for tag in repo_tags:
            repo, _, t = tag.partition(":")
            name = name or repo.removeprefix("drover/")
            tags.append(t)
        return cls(
            name=name,
            tags=tags,
            size=data.get("Size", 0),
            created=datetime.fromtimestamp(data["Created"]),
        )


class ImageDetail(ImageSummary):
    id: str
    architecture: str | None = None
    os: str | None = None

    @classmethod
    def from_docker_inspect(cls, short_name: str, data: dict) -> "ImageDetail":
        repo_tags = data.get("RepoTags") or []
        tags = []
        for tag in repo_tags:
            _, _, t = tag.partition(":")
            if t:
                tags.append(t)
        return cls(
            name=short_name,
            tags=tags,
            size=data.get("Size", 0),
            created=datetime.fromisoformat(data["Created"]),
            id=data["Id"],
            architecture=data.get("Architecture"),
            os=data.get("Os"),
        )
