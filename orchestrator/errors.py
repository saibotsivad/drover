"""Shared exception classes for worker, Docker, and log-capture layers.

All exception classes are defined here to avoid circular import issues and
to provide a single source of truth for error handling. The base classes
carry ``status_code`` and ``detail`` so the FastAPI routers can translate
any subclass into an ``HTTPException`` uniformly.
"""


class WorkerError(Exception):
    """Base exception for worker operations."""

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


class WorkerNotFoundError(DockerError):
    """Raised when a Docker container is not found."""


class WorkerConflictError(DockerError):
    """Raised on worker state conflicts (e.g. already started/stopped)."""


class WorkerNotFound(WorkerError):
    def __init__(self, worker_id: str) -> None:
        super().__init__(404, f"Worker '{worker_id}' not found")


class WorkerStateConflict(WorkerError):
    def __init__(self, worker_id: str, current: str, action: str) -> None:
        super().__init__(
            409,
            f"Cannot {action} worker '{worker_id}' in state '{current}'",
        )


class PrivilegedNotConfigured(WorkerError):
    def __init__(self) -> None:
        super().__init__(
            400,
            "Privileged workers are not configured (PRIVILEGED_IMAGE is unset)",
        )


class ImageNotFound(WorkerError):
    def __init__(self, image: str) -> None:
        super().__init__(404, f"Image '{image}' not found")


class WorkerNotConnected(WorkerError):
    def __init__(self, worker_id: str) -> None:
        super().__init__(
            409,
            f"Worker '{worker_id}' has no active worker agent connection",
        )


class CommandNotFound(WorkerError):
    def __init__(self, worker_id: str, command_id: str) -> None:
        super().__init__(
            404,
            f"Command '{command_id}' not found on worker '{worker_id}'",
        )


class CapabilityNotSupported(WorkerError):
    """Raised when a request needs a capability the image does not declare."""

    def __init__(self, capability: str) -> None:
        super().__init__(
            422,
            f"Image does not declare the required capability '{capability}'",
        )


class LoggingNotEnabled(WorkerError):
    """Raised when log-file endpoints are hit but capture is disabled."""

    def __init__(self) -> None:
        super().__init__(
            409,
            "Worker log retention is disabled "
            "(DROVER_ENABLE_WORKER_LOGS is not set to \"true\")",
        )


class LogFileNotFound(WorkerError):
    """Raised when a requested captured log file does not exist."""

    def __init__(self, filename: str) -> None:
        super().__init__(404, f"Captured log file '{filename}' not found")
