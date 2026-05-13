"""Shared exception base used by the container and log-capture layers.

Lives in its own module so that ``log_capture.py`` can raise
``ContainerError`` subclasses without importing ``container_manager``,
which imports ``log_capture`` itself.  The base class carries
``status_code`` and ``detail`` so the FastAPI routers can translate any
subclass into an ``HTTPException`` uniformly.
"""


class ContainerError(Exception):
    """Base exception for container operations."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)
