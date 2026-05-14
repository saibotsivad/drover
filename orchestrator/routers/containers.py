"""Container lifecycle REST endpoints."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from orchestrator.container_manager import (
    ContainerError,
    ContainerManager,
    ContainerNotFound,
)
from orchestrator.docker_client import ContainerNotFoundError
from orchestrator.log_capture import LogCaptureManager
from orchestrator.models import (
    ContainerResponse,
    CreateContainerRequest,
    ExecRequest,
    ExecResponse,
    ExecStatusResponse,
)

router = APIRouter(prefix="/containers", tags=["containers"])


def _manager(request: Request) -> ContainerManager:
    return request.app.state.container_manager


def _log_capture(request: Request) -> LogCaptureManager:
    return request.app.state.log_capture


async def _ensure_container_exists(request: Request, container_id: str) -> None:
    """Raise 404 if the container row does not exist.

    Used by the ``/logs/*`` routes so the 404 path is checked before any
    ``LoggingNotEnabled`` 409, keeping the error ordering predictable.
    """
    try:
        await _manager(request).get_container(container_id)
    except ContainerNotFound as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except ContainerError:
        # Any other ContainerError implies the row exists; let the
        # actual route handler surface it.
        return


@router.get("")
async def list_containers(request: Request) -> list[ContainerResponse]:
    return await _manager(request).list_containers()


@router.post("", status_code=201)
async def create_container(
    req: CreateContainerRequest, request: Request
) -> ContainerResponse:
    try:
        return await _manager(request).create_container(req)
    except ContainerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{container_id}")
async def get_container(container_id: str, request: Request) -> ContainerResponse:
    try:
        return await _manager(request).get_container(container_id)
    except ContainerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{container_id}/stop")
async def stop_container(container_id: str, request: Request) -> ContainerResponse:
    try:
        return await _manager(request).stop_container(container_id)
    except ContainerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{container_id}/resume")
async def resume_container(container_id: str, request: Request) -> ContainerResponse:
    try:
        return await _manager(request).resume_container(container_id)
    except ContainerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/{container_id}")
async def destroy_container(container_id: str, request: Request) -> ContainerResponse:
    try:
        return await _manager(request).destroy_container(container_id)
    except ContainerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{container_id}/exec", status_code=201)
async def exec_command(
    container_id: str, req: ExecRequest, request: Request
) -> ExecResponse:
    try:
        command_id = await _manager(request).exec_command(container_id, req.command)
        return ExecResponse(command_id=command_id)
    except ContainerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{container_id}/exec/{command_id}")
async def get_exec_status(
    container_id: str, command_id: str, request: Request
) -> ExecStatusResponse:
    try:
        return await _manager(request).get_command_status(container_id, command_id)
    except ContainerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# -- log access ------------------------------------------------------------
#
# Three routes for accessing container logs.  ``/logs`` is a live tail
# from Docker; ``/logs/files`` and ``/logs/files/{filename}`` expose the
# on-disk capture written by ``LogCaptureManager``.  See
# ``docs/observability.md`` for the operator-facing reference.


@router.get("/{container_id}/logs")
async def get_container_logs(
    container_id: str, request: Request, tail: str = "all"
) -> PlainTextResponse:
    """Live Docker logs (text/plain).

    Thin proxy over ``DockerClient.get_container_logs``.  Returns the
    full history captured by Docker's own log driver — the on-disk
    history captured by Drover lives behind ``/logs/files``.
    """
    await _ensure_container_exists(request, container_id)
    # Look up the docker_id via the manager so 404 ordering is predictable.
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT docker_id FROM containers WHERE id = ?", (container_id,)
    )
    if not row or not row["docker_id"]:
        raise HTTPException(
            status_code=404,
            detail=f"Container '{container_id}' has no associated Docker container",
        )
    docker = request.app.state.docker
    try:
        body = await docker.get_container_logs(row["docker_id"], tail=tail)
    except ContainerNotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
    return PlainTextResponse(content=body)


@router.get("/{container_id}/logs/files")
async def list_log_files(container_id: str, request: Request) -> list[str]:
    """List captured log filenames for a container.

    Returns ``[]`` when retention is enabled but the container has no
    captured logs (e.g. discarded after destroy, or row predates
    ``DROVER_LOG_DIR``).  Returns 409 when retention is disabled.
    """
    await _ensure_container_exists(request, container_id)
    try:
        return _log_capture(request).list_files(container_id)
    except ContainerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{container_id}/logs/orchestrator")
async def get_orchestrator_logs(
    container_id: str, request: Request
) -> PlainTextResponse:
    """Orchestrator's own Docker logs, filtered to a single container.

    Uses the same Docker log proxy as ``/logs`` but targets the
    orchestrator's container ID (auto-detected at startup) and filters
    line-by-line on substring match with ``container_id``. The 26-char
    ULID format makes false-positive matches negligible.
    """
    await _ensure_container_exists(request, container_id)
    own_id = request.app.state.orchestrator_docker_id
    if own_id is None:
        raise HTTPException(
            status_code=503,
            detail="Orchestrator container ID could not be detected",
        )
    docker = request.app.state.docker
    try:
        body = await docker.get_container_logs(own_id, tail="all")
    except ContainerNotFoundError as e:
        raise HTTPException(status_code=503, detail=e.message)
    lines = [l for l in body.splitlines(keepends=True) if container_id in l]
    return PlainTextResponse(content="".join(lines))


@router.get("/{container_id}/logs/files/{filename}")
async def get_log_file(
    container_id: str, filename: str, request: Request
) -> FileResponse:
    """Return a single captured log file verbatim (text/plain).

    Reads can race with the in-progress writer on the currently-active
    file; callers may see a truncated final line.  Each complete record
    is newline-terminated so log shippers handle the truncation
    gracefully — see ``docs/observability.md``.
    """
    await _ensure_container_exists(request, container_id)
    try:
        path = _log_capture(request).file_path(container_id, filename)
    except ContainerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return FileResponse(path, media_type="text/plain")
