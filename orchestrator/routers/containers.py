"""Container lifecycle REST endpoints."""

from fastapi import APIRouter, HTTPException, Request

from orchestrator.container_manager import ContainerError, ContainerManager
from orchestrator.models import (
    ContainerLogsResponse,
    ContainerResponse,
    CreateContainerRequest,
    ExecRequest,
    ExecResponse,
    ExecStatusResponse,
)

router = APIRouter(prefix="/containers", tags=["containers"])


def _manager(request: Request) -> ContainerManager:
    return request.app.state.container_manager


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


@router.get("/{container_id}/logs")
async def get_container_logs(
    container_id: str, request: Request
) -> ContainerLogsResponse:
    try:
        return await _manager(request).get_logs(container_id)
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
