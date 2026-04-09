import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

# Image names: alphanumeric start, then alphanumeric/dots/hyphens/underscores.
# Slashes separate path components (e.g. "myorg/myimage").  Each component
# must start with an alphanumeric character.
_IMAGE_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?(/[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?)*$")
_IMAGE_MAX_LEN = 256

# Labels: printable characters only (no control chars), generous max length.
_LABEL_MAX_LEN = 1024

# Environment variable keys: POSIX-style identifiers.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_KEY_MAX_LEN = 256
_ENV_VALUE_MAX_LEN = 32_768  # 32 KB

# Timeout bounds: minimum 1 second (via gt=0), maximum 24 hours.
_TIMEOUT_MAX = 86_400


# --- Container models ---


class ContainerStatus(str, Enum):
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    resuming = "resuming"
    destroying = "destroying"
    destroyed = "destroyed"


class CreateContainerRequest(BaseModel):
    image: str = Field(max_length=_IMAGE_MAX_LEN)
    privileged: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    label: str | None = Field(default=None, max_length=_LABEL_MAX_LEN)
    timeout_seconds: int = Field(default=300, gt=0, le=_TIMEOUT_MAX)

    @field_validator("image")
    @classmethod
    def validate_image(cls, v: str) -> str:
        if not _IMAGE_RE.match(v):
            raise ValueError(
                "Image name must contain only alphanumeric characters, dots, "
                "hyphens, and underscores, with slashes separating path "
                "components. Each component must start and end with an "
                "alphanumeric character."
            )
        return v

    @field_validator("label")
    @classmethod
    def validate_label(cls, v: str | None) -> str | None:
        if v is not None and any(
            c != "\n" and c != "\t" and (c < " " or c == "\x7f") for c in v
        ):
            raise ValueError(
                "Label must contain only printable characters "
                "(tabs and newlines are allowed)."
            )
        return v

    @field_validator("env")
    @classmethod
    def validate_env(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if len(key) > _ENV_KEY_MAX_LEN:
                raise ValueError(
                    f"Environment variable key '{key[:64]}...' exceeds "
                    f"maximum length of {_ENV_KEY_MAX_LEN} characters."
                )
            if not _ENV_KEY_RE.match(key):
                raise ValueError(
                    f"Environment variable key '{key}' is invalid. Keys must "
                    "start with a letter or underscore and contain only "
                    "letters, digits, and underscores."
                )
            if len(value) > _ENV_VALUE_MAX_LEN:
                raise ValueError(
                    f"Environment variable value for '{key}' exceeds "
                    f"maximum length of {_ENV_VALUE_MAX_LEN} characters."
                )
        return v


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
