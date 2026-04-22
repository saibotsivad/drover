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


# -- helpers ----------------------------------------------------------------


async def _await_init(manager, container_id):
    """Wait for the background init task and cancel the watchdog."""
    task = manager._init_tasks.get(container_id)
    if task:
        await task
    await manager.on_container_ready(container_id)


async def _create_running(manager, db, **kwargs):
    """Create a container and simulate its guest agent sending ``ready``.

    Many tests need a container in ``running`` state to exercise stop,
    resume, exec, and destroy paths.  This helper awaits the background
    init, cancels the watchdog, and flips the DB row to ``running``.
    """
    resp = await manager.create_container(CreateContainerRequest(**kwargs))
    await _await_init(manager, resp.id)
    await db.execute_insert(
        "UPDATE containers SET status = 'running' "
        "WHERE id = ? AND status = 'initializing'",
        (resp.id,),
    )
    return resp


# -- create -----------------------------------------------------------------


async def test_create_container_returns_initializing(manager, docker, sockets, db):
    """create_container returns immediately with status=initializing."""
    resp = await manager.create_container(
        CreateContainerRequest(image="python-runner", timeout_seconds=300)
    )
    assert resp.image == "python-runner"
    assert resp.status == ContainerStatus.initializing
    assert resp.privileged is False
    assert resp.timeout_seconds == 300
    assert resp.error_code is None

    # Phase 1 validated the image synchronously
    docker.inspect_image.assert_called_once_with("drover/python-runner")

    # Phase 2 runs in the background
    await _await_init(manager, resp.id)
    docker.create_container.assert_called_once()
    docker.start_container.assert_called_once_with("docker_abc123")
    sockets.create_socket.assert_called_once_with(resp.id)

    # Remains initializing until the guest agent sends ready
    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.initializing


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
        init_timeout_seconds=config.init_timeout_seconds,
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
    await _await_init(mgr, resp.id)


async def test_create_docker_failure_transitions_to_error(
    manager, docker, sockets, db
):
    """Docker start failure transitions the container to error with init_docker_error."""
    docker.start_container.side_effect = RuntimeError("docker start failed")
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await _await_init(manager, resp.id)

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.error
    assert fetched.error_code == "init_docker_error"

    # Cleanup ran: socket destroyed, Docker container force-removed
    sockets.destroy_socket.assert_called_once_with(resp.id)
    docker.remove_container.assert_called_once_with("docker_abc123", force=True)


async def test_ready_transitions_to_running_via_callback(
    manager, docker, sockets, db
):
    """A successful ready update + callback yields status=running and no watchdog."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await _await_init(manager, resp.id)

    # Simulate the socket manager handling a ready message
    await db.execute_insert(
        "UPDATE containers SET status = 'running' "
        "WHERE id = ? AND status = 'initializing'",
        (resp.id,),
    )
    # Watchdog should already be gone (cancelled by _await_init), but calling
    # again must be safe.
    await manager.on_container_ready(resp.id)

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.running
    assert resp.id not in manager._init_watchdogs


# -- get --------------------------------------------------------------------


async def test_get_container(manager, db):
    resp = await _create_running(manager, db, image="test-img")
    fetched = await manager.get_container(resp.id)
    assert fetched.id == resp.id
    assert fetched.status == ContainerStatus.running


async def test_get_container_not_found(manager):
    with pytest.raises(ContainerNotFound):
        await manager.get_container("NONEXISTENT0000000000000000")


async def test_get_container_syncs_with_docker_stopped(manager, docker, db):
    """If Docker says container is stopped but DB says running, sync it."""
    resp = await _create_running(manager, db, image="test-img")
    docker.inspect_container.return_value = {"State": {"Status": "exited"}}

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.stopped


async def test_get_container_docker_gone_marks_destroyed(manager, docker, db):
    resp = await _create_running(manager, db, image="test-img")
    docker.inspect_container.side_effect = ContainerNotFoundError(404, "gone")

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.destroyed


async def test_get_container_initializing_does_not_touch_docker(
    manager, docker, db
):
    """While initializing, get_container does not try to sync with Docker."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await _await_init(manager, resp.id)
    docker.inspect_container.reset_mock()

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.initializing
    docker.inspect_container.assert_not_called()


# -- stop -------------------------------------------------------------------


async def test_stop_container(manager, docker, sockets, db):
    resp = await _create_running(manager, db, image="test-img")
    stopped = await manager.stop_container(resp.id)
    assert stopped.status == ContainerStatus.stopped
    assert stopped.stopped_at is not None
    sockets.close_socket.assert_called_once_with(resp.id)
    docker.stop_container.assert_called_once()


async def test_stop_already_stopped_raises_conflict(manager, db):
    resp = await _create_running(manager, db, image="test-img")
    await manager.stop_container(resp.id)

    with pytest.raises(ContainerStateConflict):
        await manager.stop_container(resp.id)


async def test_stop_initializing_raises_conflict(manager, db):
    """Cannot stop a container that is still initializing."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await _await_init(manager, resp.id)

    with pytest.raises(ContainerStateConflict):
        await manager.stop_container(resp.id)


# -- resume -----------------------------------------------------------------


async def test_resume_container(manager, docker, sockets, db):
    resp = await _create_running(manager, db, image="test-img")
    await manager.stop_container(resp.id)

    # Reset mocks so we can verify resume-specific calls
    sockets.create_socket.reset_mock()
    docker.start_container.reset_mock()

    resumed = await manager.resume_container(resp.id)
    assert resumed.status == ContainerStatus.running
    assert resumed.stopped_at is None
    sockets.create_socket.assert_called_once_with(resp.id)
    docker.start_container.assert_called_once()


async def test_resume_running_raises_conflict(manager, db):
    resp = await _create_running(manager, db, image="test-img")
    with pytest.raises(ContainerStateConflict):
        await manager.resume_container(resp.id)


# -- destroy ----------------------------------------------------------------


async def test_destroy_running_container(manager, docker, sockets, db):
    resp = await _create_running(manager, db, image="test-img")
    destroyed = await manager.destroy_container(resp.id)
    assert destroyed.status == ContainerStatus.destroyed
    sockets.destroy_socket.assert_called_once_with(resp.id)
    docker.remove_container.assert_called_once()


async def test_destroy_stopped_container(manager, docker, sockets, db):
    resp = await _create_running(manager, db, image="test-img")
    await manager.stop_container(resp.id)
    sockets.destroy_socket.reset_mock()

    destroyed = await manager.destroy_container(resp.id)
    assert destroyed.status == ContainerStatus.destroyed
    assert destroyed.stopped_at is not None  # preserves original stopped_at


async def test_destroy_already_destroyed_raises_conflict(manager, db):
    resp = await _create_running(manager, db, image="test-img")
    await manager.destroy_container(resp.id)
    with pytest.raises(ContainerStateConflict):
        await manager.destroy_container(resp.id)


async def test_destroy_error_container_without_docker_id(
    manager, docker, sockets, db
):
    """An errored container with no docker_id can still be destroyed."""
    docker.create_container.side_effect = RuntimeError("boom")
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await _await_init(manager, resp.id)

    # Sanity: errored with no docker_id persisted
    row = await db.fetchone(
        "SELECT docker_id, status FROM containers WHERE id = ?", (resp.id,)
    )
    assert row["docker_id"] is None
    assert row["status"] == "error"

    docker.stop_container.reset_mock()
    docker.remove_container.reset_mock()

    destroyed = await manager.destroy_container(resp.id)
    assert destroyed.status == ContainerStatus.destroyed
    # No docker calls attempted when docker_id is NULL
    docker.stop_container.assert_not_called()
    docker.remove_container.assert_not_called()


# -- exec -------------------------------------------------------------------


async def test_exec_command(manager, sockets, db):
    resp = await _create_running(manager, db, image="test-img")
    cmd_id = await manager.exec_command(resp.id, "echo hello")
    assert len(cmd_id) == 26
    sockets.send_command.assert_called_once()


async def test_exec_on_stopped_raises_conflict(manager, db):
    resp = await _create_running(manager, db, image="test-img")
    await manager.stop_container(resp.id)
    with pytest.raises(ContainerStateConflict):
        await manager.exec_command(resp.id, "echo hello")


async def test_exec_on_initializing_raises_conflict(manager, db):
    """Exec must fail with 409 while the container is still initializing."""
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await _await_init(manager, resp.id)

    with pytest.raises(ContainerStateConflict):
        await manager.exec_command(resp.id, "echo hello")


async def test_exec_not_connected_raises(manager, sockets, db):
    resp = await _create_running(manager, db, image="test-img")
    sockets.is_connected.return_value = False
    with pytest.raises(ContainerNotConnected):
        await manager.exec_command(resp.id, "echo hello")


# -- get command status -----------------------------------------------------


async def test_get_command_status(manager, db):
    resp = await _create_running(manager, db, image="test-img")
    cmd_id = await manager.exec_command(resp.id, "echo hello")
    status = await manager.get_command_status(resp.id, cmd_id)
    assert status.command_id == cmd_id
    assert status.status.value == "pending"
    assert status.messages == []


# -- sync_containers (startup reconciliation) ------------------------------


async def test_sync_running_container_still_running(manager, docker, sockets, db):
    """Running container still alive in Docker → re-establish socket listener."""
    resp = await _create_running(manager, db, image="test-img")
    sockets.create_socket.reset_mock()

    # Docker says still running
    docker.inspect_container.return_value = {"State": {"Status": "running"}}
    await manager.sync_containers()

    sockets.create_socket.assert_called_once_with(resp.id)
    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.running


async def test_sync_running_container_now_stopped(manager, docker, sockets, db):
    """Running in DB but Docker says exited → update to stopped, no socket."""
    resp = await _create_running(manager, db, image="test-img")
    sockets.create_socket.reset_mock()

    docker.inspect_container.return_value = {"State": {"Status": "exited"}}
    await manager.sync_containers()

    sockets.create_socket.assert_not_called()
    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.stopped


async def test_sync_running_container_gone(manager, docker, sockets, db):
    """Running in DB but Docker container gone → mark destroyed."""
    resp = await _create_running(manager, db, image="test-img")
    docker.inspect_container.side_effect = ContainerNotFoundError(404, "gone")
    await manager.sync_containers()

    # Reset side_effect so get_container can re-fetch from DB
    docker.inspect_container.side_effect = None
    docker.inspect_container.return_value = {"State": {"Status": "running"}}

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.destroyed


async def test_sync_stopped_container_still_stopped(manager, docker, sockets, db):
    """Stopped in DB and Docker agrees → no status change, no socket."""
    resp = await _create_running(manager, db, image="test-img")
    await manager.stop_container(resp.id)
    sockets.create_socket.reset_mock()

    docker.inspect_container.return_value = {"State": {"Status": "exited"}}
    await manager.sync_containers()

    sockets.create_socket.assert_not_called()
    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.stopped


async def test_sync_stopped_container_gone(manager, docker, sockets, db):
    """Stopped in DB but Docker container gone → mark destroyed."""
    resp = await _create_running(manager, db, image="test-img")
    await manager.stop_container(resp.id)

    docker.inspect_container.side_effect = ContainerNotFoundError(404, "gone")
    await manager.sync_containers()

    docker.inspect_container.side_effect = None
    docker.inspect_container.return_value = {"State": {"Status": "running"}}

    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.destroyed


async def test_sync_skips_destroyed_containers(manager, docker, sockets, db):
    """Already-destroyed containers should not be inspected at all."""
    resp = await _create_running(manager, db, image="test-img")
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
    resp = await _create_running(manager, db, image="test-img")
    # Manually set to a transitional state
    await db.execute_insert(
        "UPDATE containers SET status = 'stopping' WHERE id = ?",
        (resp.id,),
    )

    docker.inspect_container.return_value = {"State": {"Status": "exited"}}
    await manager.sync_containers()

    row = await db.fetchone("SELECT status FROM containers WHERE id = ?", (resp.id,))
    assert row["status"] == "stopping"


async def test_sync_initializing_rows_become_error(manager, docker, sockets, db):
    """Containers stuck in initializing after restart are transitioned to error."""
    # Create a container but do not send ready — it stays in initializing.
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await _await_init(manager, resp.id)

    # Manually clear the init task bookkeeping to simulate an orchestrator
    # restart where the previous process's tasks are gone.
    manager._init_tasks.clear()
    manager._init_watchdogs.clear()

    docker.inspect_container.reset_mock()
    await manager.sync_containers()

    # sync_containers should skip the initializing row during reconciliation
    # and not inspect Docker for it.
    docker.inspect_container.assert_not_called()

    # After the pass, the row is transitioned to error with orchestrator_crash.
    fetched = await manager.get_container(resp.id)
    assert fetched.status == ContainerStatus.error
    assert fetched.error_code == "orchestrator_crash"


async def test_sync_skips_error_containers(manager, docker, sockets, db):
    """Containers already in error are not reconciled."""
    docker.start_container.side_effect = RuntimeError("boom")
    resp = await manager.create_container(
        CreateContainerRequest(image="test-img")
    )
    await _await_init(manager, resp.id)
    docker.inspect_container.reset_mock()

    await manager.sync_containers()
    docker.inspect_container.assert_not_called()


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
