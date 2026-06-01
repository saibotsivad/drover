"""Worker lifecycle REST endpoints."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from orchestrator.worker_manager import WorkerManager
from orchestrator.errors import (
    WorkerError,
    WorkerNotFound,
    WorkerNotFoundError,
)
from orchestrator.log_capture import LogCaptureManager
from orchestrator.models import (
    CommandSummary,
    WorkerResponse,
    CreateWorkerRequest,
    ExecRequest,
    ExecResponse,
    ExecStatusResponse,
)

router = APIRouter(prefix="/workers", tags=["workers"])


def _manager(request: Request) -> WorkerManager:
    return request.app.state.worker_manager


def _log_capture(request: Request) -> LogCaptureManager:
    return request.app.state.log_capture


async def _ensure_worker_exists(request: Request, worker_id: str) -> None:
    """Raise 404 if the worker row does not exist.

    Used by the ``/logs/*`` routes so the 404 path is checked before any
    ``LoggingNotEnabled`` 409, keeping the error ordering predictable.
    """
    try:
        await _manager(request).get_worker(worker_id)
    except WorkerNotFound as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except WorkerError:
        # Any other WorkerError implies the row exists; let the
        # actual route handler surface it.
        return


@router.get("")
async def list_workers(request: Request) -> list[WorkerResponse]:
    return await _manager(request).list_workers()


@router.post("", status_code=201)
async def create_worker(
    req: CreateWorkerRequest, request: Request
) -> WorkerResponse:
    try:
        return await _manager(request).create_worker(req)
    except WorkerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{worker_id}")
async def get_worker(worker_id: str, request: Request) -> WorkerResponse:
    try:
        return await _manager(request).get_worker(worker_id)
    except WorkerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{worker_id}/stop")
async def stop_worker(worker_id: str, request: Request) -> WorkerResponse:
    try:
        return await _manager(request).stop_worker(worker_id)
    except WorkerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{worker_id}/resume")
async def resume_worker(worker_id: str, request: Request) -> WorkerResponse:
    try:
        return await _manager(request).resume_worker(worker_id)
    except WorkerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/{worker_id}")
async def destroy_worker(worker_id: str, request: Request) -> WorkerResponse:
    try:
        return await _manager(request).destroy_worker(worker_id)
    except WorkerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{worker_id}/execs")
async def list_commands(
    worker_id: str, request: Request
) -> list[CommandSummary]:
    try:
        return await _manager(request).list_commands(worker_id)
    except WorkerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{worker_id}/execs", status_code=201)
async def exec_command(
    worker_id: str, req: ExecRequest, request: Request
) -> ExecResponse:
    try:
        command_id = await _manager(request).exec_command(worker_id, req.command)
        return ExecResponse(command_id=command_id)
    except WorkerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{worker_id}/execs/{command_id}")
async def get_exec_status(
    worker_id: str, command_id: str, request: Request
) -> ExecStatusResponse:
    try:
        return await _manager(request).get_command_status(worker_id, command_id)
    except WorkerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# -- log access ------------------------------------------------------------
#
# Three routes for accessing worker logs.  ``/logs`` is a live tail
# from Docker; ``/logs/files`` and ``/logs/files/{filename}`` expose the
# on-disk capture written by ``LogCaptureManager``.  See
# ``docs/observability.md`` for the operator-facing reference.


@router.get("/{worker_id}/logs")
async def get_worker_logs(
    worker_id: str, request: Request, tail: str = "all"
) -> PlainTextResponse:
    """Live Docker logs (text/plain).

    Thin proxy over ``DockerClient.get_container_logs``.  Returns the
    full history captured by Docker's own log driver — the on-disk
    history captured by Drover lives behind ``/logs/files``.
    """
    await _ensure_worker_exists(request, worker_id)
    # Look up the docker_id via the manager so 404 ordering is predictable.
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT docker_id FROM workers WHERE id = ?", (worker_id,)
    )
    if not row or not row["docker_id"]:
        raise HTTPException(
            status_code=404,
            detail=f"Worker '{worker_id}' has no associated Docker container",
        )
    docker = request.app.state.docker
    try:
        body = await docker.get_container_logs(row["docker_id"], tail=tail)
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
    return PlainTextResponse(content=body)


@router.get("/{worker_id}/logs/files")
async def list_log_files(worker_id: str, request: Request) -> list[str]:
    """List captured log filenames for a worker.

    Returns ``[]`` when retention is enabled but the worker has no
    captured logs (e.g. discarded after destroy, or row predates
    ``DROVER_ENABLE_WORKER_LOGS=true``).  Returns 409 when retention
    is disabled.
    """
    await _ensure_worker_exists(request, worker_id)
    try:
        return _log_capture(request).list_files(worker_id)
    except WorkerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{worker_id}/logs/orchestrator")
async def get_orchestrator_logs(
    worker_id: str, request: Request
) -> PlainTextResponse:
    """Orchestrator's own Docker logs, filtered to a single worker.

    Uses the same Docker log proxy as ``/logs`` but targets the
    orchestrator's container ID (auto-detected at startup) and filters
    line-by-line on substring match with ``worker_id``. The 26-char
    ULID format makes false-positive matches negligible.
    """
    await _ensure_worker_exists(request, worker_id)
    own_id = request.app.state.orchestrator_docker_id
    if own_id is None:
        raise HTTPException(
            status_code=503,
            detail="Orchestrator container ID could not be detected",
        )
    docker = request.app.state.docker
    try:
        body = await docker.get_container_logs(own_id, tail="all")
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=503, detail=e.message)
    lines = [l for l in body.splitlines(keepends=True) if worker_id in l]
    return PlainTextResponse(content="".join(lines))


@router.get("/{worker_id}/logs/files/{filename}")
async def get_log_file(
    worker_id: str, filename: str, request: Request
) -> FileResponse:
    """Return a single captured log file verbatim (text/plain).

    Reads can race with the in-progress writer on the currently-active
    file; callers may see a truncated final line.  Each complete record
    is newline-terminated so log shippers handle the truncation
    gracefully — see ``docs/observability.md``.
    """
    await _ensure_worker_exists(request, worker_id)
    try:
        path = _log_capture(request).file_path(worker_id, filename)
    except WorkerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return FileResponse(path, media_type="text/plain")
