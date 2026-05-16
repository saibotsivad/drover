"""Shared exception classes for container, Docker, and log-capture layers.

All exception classes are defined here to avoid circular import issues and
to provide a single source of truth for error handling. The base classes
carry ``status_code`` and ``detail`` so the FastAPI routers can translate
any subclass into an ``HTTPException`` uniformly.
"""


class ContainerError(Exception):
    """Base exception for container operations."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class DockerError(Exception):
    """Base exception for Docker API errors."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Docker API error ({status_code}): {message}")


class ImageNotFoundError(DockerError):
    """Raised when a Docker image is not found."""


class ContainerNotFoundError(DockerError):
    """Raised when a Docker container is not found."""


class ContainerConflictError(DockerError):
    """Raised on container state conflicts (e.g. already started/stopped)."""


class ContainerNotFound(ContainerError):
    def __init__(self, container_id: str) -> None:
        super().__init__(404, f"Container '{container_id}' not found")


class ContainerStateConflict(ContainerError):
    def __init__(self, container_id: str, current: str, action: str) -> None:
        super().__init__(
            409,
            f"Cannot {action} container '{container_id}' in state '{current}'",
        )


class PrivilegedNotConfigured(ContainerError):
    def __init__(self) -> None:
        super().__init__(
            400,
            "Privileged containers are not configured (DROVER_PRIVILEGED_IMAGE is unset)",
        )


class ImageNotFound(ContainerError):
    def __init__(self, image: str) -> None:
        super().__init__(404, f"Image '{image}' not found")


class ContainerNotConnected(ContainerError):
    def __init__(self, container_id: str) -> None:
        super().__init__(
            409,
            f"Container '{container_id}' has no active guest agent connection",
        )


class CommandNotFound(ContainerError):
    def __init__(self, container_id: str, command_id: str) -> None:
        super().__init__(
            404,
            f"Command '{command_id}' not found on container '{container_id}'",
        )


class LoggingNotEnabled(ContainerError):
    """Raised when log-file endpoints are hit but DROVER_LOG_DIR is unset."""

    def __init__(self) -> None:
        super().__init__(
            409,
            "Container log retention is disabled (DROVER_LOG_DIR is unset)",
        )


class LogFileNotFound(ContainerError):
    """Raised when a requested captured log file does not exist."""

    def __init__(self, filename: str) -> None:
        super().__init__(404, f"Captured log file '{filename}' not found")
