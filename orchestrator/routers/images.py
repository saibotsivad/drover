from fastapi import APIRouter, HTTPException, Request

from orchestrator.docker_client import DockerClient, ImageNotFoundError
from orchestrator.models import ImageDetail, ImageSummary

router = APIRouter(prefix="/images", tags=["images"])


@router.get("")
async def list_images(request: Request) -> list[ImageSummary]:
    docker: DockerClient = request.app.state.docker
    raw = await docker.list_images()
    return [ImageSummary.from_docker(img) for img in raw]


@router.get("/{name}")
async def get_image(name: str, request: Request) -> ImageDetail:
    docker: DockerClient = request.app.state.docker
    try:
        data = await docker.inspect_image(f"drover/{name}")
    except ImageNotFoundError:
        raise HTTPException(status_code=404, detail=f"Image '{name}' not found")
    return ImageDetail.from_docker_inspect(name, data)
