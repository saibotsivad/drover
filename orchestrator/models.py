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
    initializing = "initializing"
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    resuming = "resuming"
    destroying = "destroying"
    destroyed = "destroyed"
    error = "error"


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
    # Populated only when status == 'error'; identifies the failure cause.
    error_code: str | None = None


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
    command: str
    status: CommandStatus
    exit_code: int | None = None
    messages: list[CommandMessage] = Field(default_factory=list)


class CommandSummary(BaseModel):
    command_id: str
    command: str
    status: CommandStatus
    exit_code: int | None = None
    created_at: str


# --- Image models ---


class ImageSummary(BaseModel):
    name: str
    tags: list[str]
    labels: dict[str, str] = Field(default_factory=dict)
    size: int
    created: datetime

    @classmethod
    def from_docker(cls, data: dict) -> "ImageSummary":
        # Identity comes from the ``drover.*`` label set; ``RepoTags`` is
        # surfaced as informational metadata only.
        labels = _drover_labels(data.get("Labels"))
        tags = []
        for tag in data.get("RepoTags") or []:
            _, _, t = tag.partition(":")
            if t:
                tags.append(t)
        return cls(
            name=labels.get("drover.name", ""),
            tags=tags,
            labels=labels,
            size=data.get("Size", 0),
            created=datetime.fromtimestamp(data["Created"]),
        )


class ImageDetail(ImageSummary):
    id: str
    architecture: str | None = None
    os: str | None = None

    @classmethod
    def from_docker_inspect(cls, data: dict) -> "ImageDetail":
        labels = _drover_labels((data.get("Config") or {}).get("Labels"))
        tags = []
        for tag in data.get("RepoTags") or []:
            _, _, t = tag.partition(":")
            if t:
                tags.append(t)
        return cls(
            name=labels.get("drover.name", ""),
            tags=tags,
            labels=labels,
            size=data.get("Size", 0),
            created=datetime.fromisoformat(data["Created"]),
            id=data["Id"],
            architecture=data.get("Architecture"),
            os=data.get("Os"),
        )


def _drover_labels(raw: dict | None) -> dict[str, str]:
    """Return only the ``drover.*`` labels from a Docker label dict.

    Filters non-Drover labels so the response doesn't leak unrelated
    operator-defined metadata (e.g. org.opencontainers, vendor-specific
    tooling) that has no meaning to Drover callers.
    """
    if not raw:
        return {}
    return {k: v for k, v in raw.items() if k.startswith("drover.")}
