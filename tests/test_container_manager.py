"""Tests for ContainerManager state machine logic with mocked Docker and sockets."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.container_manager import (
    ContainerManager,
    ContainerNotConnected,
    ContainerNotFound,
    ContainerStateConflict,
    ImageNotFound,
    PrivilegedNotConfigured,
    _docker_state_to_status,
)
from orchestrator.docker_client import ContainerNotFoundError, ImageNotFoundError
from orchestrator.models import ContainerStatus, CreateContainerRequest


@pytest.fixture
def docker():
    mock = AsyncMock()
    mock.inspect_image = AsyncMock(return_value={})
    mock.create_container = AsyncMock(return_value={"Id": "docker_abc123"})
    mock.start_container = AsyncMock()
    mock.stop_container = AsyncMock()
    mock.remove_container = AsyncMock()
    mock.inspect_container = AsyncMock(
        return_value={"State": {"Status": "running"}}
    )
    return mock


@pytest.fixture
def sockets():
    mock = AsyncMock()
    mock.create_socket = AsyncMock(return_value="/tmp/sockets/test.sock")
    mock.close_socket = AsyncMock()
    mock.destroy_socket = AsyncMock()
    mock.send_command = AsyncMock()
    mock.is_connected = MagicMock(return_value=True)
    return mock


@pytest.fixture
def manager(config, db, docker, sockets):
    return ContainerManager(config, db, docker, sockets)


# -- create -----------------------------------------------------------------


async def test_create_container(manager, docker, sockets, db):
    resp = await manager.create_container(
        CreateContainerRequest(image="python-runner", timeout_seconds=300)
    )
    assert resp.image == "python-runner"
    assert resp.status == ContainerStatus.running
    assert resp.privileged is False
    assert resp.timeout_seconds == 300

    docker.inspect_image.assert_called_once_with("drover/python-runner")
    docker.create_container.assert_called_once()
    docker.start_container.assert_called_once_with("docker_abc123")
    sockets.create_socket.assert_called_once()


async def test_create_container_image_not_found(manager, docker):
    docker.inspect_image.side_effect = ImageNotFoundError(404, "not found")
    with pytest.raises(ImageNotFound):
        await manager.create_container(
            CreateContainerRequest(image="nonexistent")
        )


async def test_create_privileged_not_configured(manager):
    with pytest.raises(PrivilegedNotConfigured):
        await manager.create_container(
            CreateContainerRequest(image="test", privileged=True)
        )


async def test_create_privileged_with_config(config, db, docker, sockets):
    from orchestrator.config import Config

    priv_config = Config(
        privileged_image="my-priv-image",
        db_path=config.db_path,
        socket_dir=config.socket_dir,
        docker_sock=config.docker_sock,
        reaper_interval_seconds=config.reaper_interval_seconds,
        log_level=config.log_level,
        api_key_hash=None,
    )
    mgr = ContainerManager(priv_config, db, docker, sockets)
    resp = await mgr.create_container(
        CreateContainerRequest(image="ignored", privileged=True)
    )
    assert resp.privileged is True
    # Should NOT inspect the drover/ image, since privileged uses the configured image
    docker.inspect_image.assert_not_called()


# -- get --------------------------------------------------------------------


async def test_get_container(manager, db):
    # Create a container first
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    fetched = await manager.get_container(resp.id)
    assert fetched.id == resp.id
    assert fetched.status == ContainerStatus.running


async def test_get_container_not_found(manager):
    with pytest.raises(ContainerNotFound):
        await manager.get_container("NONEXISTENT0000000000000000")


async def test_get_container_syncs_with_docker_stopped(manager, docker, db):
    """If Docker says container is stopped but DB says running, sync it."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    docker.inspect_container.return_value = {"State": {"Status": "exited"}}

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.stopped


async def test_get_container_docker_gone_marks_destroyed(manager, docker, db):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    docker.inspect_container.side_effect = ContainerNotFoundError(404, "gone")

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.destroyed


# -- stop -------------------------------------------------------------------


async def test_stop_container(manager, docker, sockets):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    stopped = await manager.stop_container(resp.id)
    assert stopped.status == ContainerStatus.stopped
    assert stopped.stopped_at is not None
    sockets.close_socket.assert_called_once_with(resp.id)
    docker.stop_container.assert_called_once()


async def test_stop_already_stopped_raises_conflict(manager):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await manager.stop_container(resp.id)

    with pytest.raises(ContainerStateConflict):
        await manager.stop_container(resp.id)


# -- resume -----------------------------------------------------------------


async def test_resume_container(manager, docker, sockets):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await manager.stop_container(resp.id)

    # Reset mocks so we can verify resume-specific calls
    sockets.create_socket.reset_mock()
    docker.start_container.reset_mock()

    resumed = await manager.resume_container(resp.id)
    assert resumed.status == ContainerStatus.running
    assert resumed.stopped_at is None
    sockets.create_socket.assert_called_once_with(resp.id)
    docker.start_container.assert_called_once()


async def test_resume_running_raises_conflict(manager):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    with pytest.raises(ContainerStateConflict):
        await manager.resume_container(resp.id)


# -- destroy ----------------------------------------------------------------


async def test_destroy_running_container(manager, docker, sockets):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    destroyed = await manager.destroy_container(resp.id)
    assert destroyed.status == ContainerStatus.destroyed
    sockets.destroy_socket.assert_called_once_with(resp.id)
    docker.remove_container.assert_called_once()


async def test_destroy_stopped_container(manager, docker, sockets):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await manager.stop_container(resp.id)
    sockets.destroy_socket.reset_mock()

    destroyed = await manager.destroy_container(resp.id)
    assert destroyed.status == ContainerStatus.destroyed
    assert destroyed.stopped_at is not None  # preserves original stopped_at


async def test_destroy_already_destroyed_raises_conflict(manager):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await manager.destroy_container(resp.id)
    with pytest.raises(ContainerStateConflict):
        await manager.destroy_container(resp.id)


# -- exec -------------------------------------------------------------------


async def test_exec_command(manager, sockets, db):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    cmd_id = await manager.exec_command(resp.id, "echo hello")
    assert len(cmd_id) == 26
    sockets.send_command.assert_called_once()


async def test_exec_on_stopped_raises_conflict(manager, sockets):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await manager.stop_container(resp.id)
    with pytest.raises(ContainerStateConflict):
        await manager.exec_command(resp.id, "echo hello")


async def test_exec_not_connected_raises(manager, sockets):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    sockets.is_connected.return_value = False
    with pytest.raises(ContainerNotConnected):
        await manager.exec_command(resp.id, "echo hello")


# -- get command status -----------------------------------------------------


async def test_get_command_status(manager, db):
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    cmd_id = await manager.exec_command(resp.id, "echo hello")
    status = await manager.get_command_status(resp.id, cmd_id)
    assert status.command_id == cmd_id
    assert status.status.value == "pending"
    assert status.messages == []


# -- sync_containers (startup reconciliation) ------------------------------


async def test_sync_running_container_still_running(manager, docker, sockets, db):
    """Running container still alive in Docker → re-establish socket listener."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    sockets.create_socket.reset_mock()

    # Docker says still running
    docker.inspect_container.return_value = {"State": {"Status": "running"}}
    await manager.sync_containers()

    sockets.create_socket.assert_called_once_with(resp.id)
    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.running


async def test_sync_running_container_now_stopped(manager, docker, sockets, db):
    """Running in DB but Docker says exited → update to stopped, no socket."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    sockets.create_socket.reset_mock()

    docker.inspect_container.return_value = {"State": {"Status": "exited"}}
    await manager.sync_containers()

    sockets.create_socket.assert_not_called()
    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.stopped


async def test_sync_running_container_gone(manager, docker, sockets, db):
    """Running in DB but Docker container gone → mark destroyed."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    docker.inspect_container.side_effect = ContainerNotFoundError(404, "gone")
    await manager.sync_containers()

    # Reset side_effect so get_container can re-fetch from DB
    docker.inspect_container.side_effect = None
    docker.inspect_container.return_value = {"State": {"Status": "running"}}

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.destroyed


async def test_sync_stopped_container_still_stopped(manager, docker, sockets, db):
    """Stopped in DB and Docker agrees → no status change, no socket."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await manager.stop_container(resp.id)
    sockets.create_socket.reset_mock()

    docker.inspect_container.return_value = {"State": {"Status": "exited"}}
    await manager.sync_containers()

    sockets.create_socket.assert_not_called()
    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.stopped


async def test_sync_stopped_container_gone(manager, docker, sockets, db):
    """Stopped in DB but Docker container gone → mark destroyed."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await manager.stop_container(resp.id)

    docker.inspect_container.side_effect = ContainerNotFoundError(404, "gone")
    await manager.sync_containers()

    docker.inspect_container.side_effect = None
    docker.inspect_container.return_value = {"State": {"Status": "running"}}

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.destroyed


async def test_sync_skips_destroyed_containers(manager, docker, sockets, db):
    """Already-destroyed containers should not be inspected at all."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await manager.destroy_container(resp.id)
    docker.inspect_container.reset_mock()

    await manager.sync_containers()
    docker.inspect_container.assert_not_called()


async def test_sync_no_containers(manager, docker, db):
    """Empty database → sync completes without errors."""
    await manager.sync_containers()
    docker.inspect_container.assert_not_called()


async def test_sync_skips_transitional_states(manager, docker, sockets, db):
    """Containers in transitional states (stopping) keep their DB status."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    # Manually set to a transitional state
    await db.execute_insert(
        "UPDATE containers SET status = 'stopping' WHERE id = ?",
        (resp.id,),
    )

    docker.inspect_container.return_value = {"State": {"Status": "exited"}}
    await manager.sync_containers()

    row = await db.fetchone("SELECT status FROM containers WHERE id = ?", (resp.id,))
    assert row["status"] == "stopping"


# -- helper: _docker_state_to_status ---------------------------------------


def test_docker_state_running():
    assert _docker_state_to_status({"State": {"Status": "running"}}) == ContainerStatus.running


def test_docker_state_exited():
    assert _docker_state_to_status({"State": {"Status": "exited"}}) == ContainerStatus.stopped


def test_docker_state_dead():
    assert _docker_state_to_status({"State": {"Status": "dead"}}) == ContainerStatus.stopped


def test_docker_state_unknown():
    assert _docker_state_to_status({"State": {"Status": "created"}}) is None


def test_docker_state_empty():
    assert _docker_state_to_status({}) is None
