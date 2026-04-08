"""Container lifecycle REST endpoints."""

from fastapi import APIRouter, HTTPException, Request

from orchestrator.container_manager import ContainerError, ContainerManager
from orchestrator.models import ContainerResponse, CreateContainerRequest

router = APIRouter(prefix="/containers", tags=["containers"])


def _manager(request: Request) -> ContainerManager:
    return request.app.state.container_manager


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
