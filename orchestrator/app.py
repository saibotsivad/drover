import asyncio
import json
import logging
import re
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from orchestrator.auth import auth_middleware
from orchestrator.config import DOCKER_SOCK, SOCKET_DIR, Config, load_config
from orchestrator.container_manager import ContainerManager
from orchestrator.database import Database
from orchestrator.docker_client import DockerClient
from orchestrator.errors import (
    ContainerNotFound,
    ContainerStateConflict,
    DockerError,
)
from orchestrator.log_capture import LogCaptureManager
from orchestrator.routers import containers, images
from orchestrator.socket_manager import SocketManager


class _JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.") + f"{record.msecs:03.0f}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    # Suppress noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


def _parse_cgroup_for_container_id(text: str) -> str | None:
    """Scan a `/proc/self/cgroup` payload for a 64-hex Docker container id.

    Returns the first match found. Two passes:

    1. Strict: a `/` or `-` delimiter immediately before the 64-hex id,
       optionally followed by `.scope`. Covers the well-known layouts
       (``/docker/<hex>``, ``/<hex>``, ``/system.slice/docker-<hex>.scope``,
       etc.) without picking up unrelated 64-hex strings.
    2. Loose: any 64-hex sequence on any line. Catches unusual nested
       layouts (kubernetes pod cgroups, custom runtimes) where the
       delimiter is something we haven't anticipated. False positives
       here would require an unrelated 64-hex string to appear inside a
       cgroup path, which is extremely unlikely in practice.
    """
    for line in text.splitlines():
        m = re.search(r"[/-]([a-f0-9]{64})(?:\.scope)?$", line)
        if m:
            return m.group(1)
    for line in text.splitlines():
        m = re.search(r"([a-f0-9]{64})", line)
        if m:
            return m.group(1)
    return None


def _parse_mountinfo_for_container_id(text: str) -> str | None:
    """Find a Docker container id in `/proc/self/mountinfo`.

    Docker bind-mounts ``/etc/hostname``, ``/etc/hosts``, and
    ``/etc/resolv.conf`` from ``/var/lib/docker/containers/<id>/`` into
    every container, which leaves an unmistakable ``/containers/<id>/``
    substring in the mountinfo regardless of cgroup driver, cgroup
    version, or hostname configuration. This is the same signal used by
    cAdvisor, the AWS ECS agent, and similar self-introspecting tools.
    """
    m = re.search(r"/containers/([a-f0-9]{64})/", text)
    if m:
        return m.group(1)
    return None


def _hostname_as_container_id() -> str | None:
    """Return the hostname when it looks like a Docker short/long container id.

    Docker sets the container hostname to the first 12 chars of the
    container id by default, and accepts that prefix as a valid container
    reference in every API call. Useful when neither `/proc/self/cgroup`
    nor `/proc/self/mountinfo` yields a match (rootless setups, exotic
    runtimes) AND the operator hasn't overridden `--hostname` to a
    non-id value such as a Compose service name.

    Returns ``None`` when the hostname doesn't match the expected
    container-id shape so we don't accidentally pass an arbitrary
    operator-chosen hostname to the Docker API.
    """
    try:
        host = socket.gethostname()
    except OSError:
        return None
    if re.fullmatch(r"[a-f0-9]{12,64}", host):
        return host
    return None


def _detect_own_container_id() -> str | None:
    """Return this process's Docker container ID.

    Tries three independent signals, in order of authoritativeness:

    1. `/proc/self/cgroup` — full 64-hex id on standard Docker layouts.
    2. `/proc/self/mountinfo` — full 64-hex id via the always-present
       ``/var/lib/docker/containers/<id>/`` bind mounts.
    3. Container hostname — short 12-hex id when neither cgroup nor
       mountinfo yields a match.

    Returns ``None`` when all three strategies fail; the caller logs a
    warning (including the cgroup, mountinfo, and hostname snapshots)
    and the ``/logs/orchestrator`` endpoint then returns 503.
    """
    try:
        cgroup_text = Path("/proc/self/cgroup").read_text()
    except OSError:
        cgroup_text = None
    if cgroup_text is not None:
        cid = _parse_cgroup_for_container_id(cgroup_text)
        if cid is not None:
            return cid

    try:
        mountinfo_text = Path("/proc/self/mountinfo").read_text()
    except OSError:
        mountinfo_text = None
    if mountinfo_text is not None:
        cid = _parse_mountinfo_for_container_id(mountinfo_text)
        if cid is not None:
            return cid

    return _hostname_as_container_id()


async def _reaper_loop(
    config: Config, db: Database, container_manager: ContainerManager
) -> None:
    """Background task: stop containers that have exceeded their idle timeout."""
    while True:
        await asyncio.sleep(config.reaper_interval_seconds)
        try:
            rows = await db.fetchall(
                "SELECT id, last_seen, timeout_seconds FROM containers "
                "WHERE status = 'running' AND last_seen IS NOT NULL AND timeout_seconds > 0",
            )
            now = datetime.now(timezone.utc)
            for row in rows:
                last_seen = datetime.fromisoformat(row["last_seen"])
                elapsed = (now - last_seen).total_seconds()
                if elapsed > row["timeout_seconds"]:
                    logger.info(
                        "Container %s timed out (last_seen=%s, elapsed=%.1fs, timeout=%ds), stopping",
                        row["id"],
                        row["last_seen"],
                        elapsed,
                        row["timeout_seconds"],
                    )
                    try:
                        await container_manager.stop_container(row["id"])
                    except ContainerStateConflict:
                        # State changed between query and stop (e.g. already stopping)
                        logger.debug(
                            "Container %s state changed before reaper could stop it",
                            row["id"],
                        )
                    except ContainerNotFound:
                        logger.debug(
                            "Container %s not found when reaper tried to stop it",
                            row["id"],
                        )
                    except Exception:
                        logger.exception(
                            "Reaper failed to stop container %s", row["id"]
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reaper loop encountered an error")


async def _resolve_host_docker_sock(
    docker: DockerClient, own_id: str | None
) -> str:
    """Return the host-side path of the Docker daemon socket.

    The orchestrator passes this string to Docker as the bind SOURCE
    when launching privileged micro-containers.  Docker resolves bind
    sources against the host filesystem, not the orchestrator's view
    of its own filesystem, so the in-container path
    (``/var/run/docker.sock``) is wrong when the host socket lives
    elsewhere (rootless Docker: ``/run/user/$UID/docker.sock``).

    We discover the right value by self-inspecting: the orchestrator
    asks Docker for its own container's record and reads the ``Source``
    of the mount whose ``Destination`` is ``/var/run/docker.sock``.
    When self-inspect can't run (own container id detection failed) or
    no matching mount is present, we fall back to
    ``/var/run/docker.sock`` and warn — that fallback only happens to
    be correct on rootful Docker.
    """
    if own_id is not None:
        try:
            info = await docker.inspect_container(own_id)
            for mount in info.get("Mounts", []) or []:
                if mount.get("Destination") == DOCKER_SOCK:
                    source = mount.get("Source")
                    if source:
                        logger.info(
                            "Host Docker socket path from self-inspect: %s",
                            source,
                        )
                        return source
            logger.warning(
                "Self-inspect succeeded but no mount with destination %s "
                "was found; privileged container launches may fail.",
                DOCKER_SOCK,
            )
        except Exception:
            logger.exception(
                "Self-inspect failed while resolving host Docker socket path"
            )
    logger.warning(
        "Falling back to %s as the host Docker socket path. This only "
        "happens to be correct on rootful Docker; on rootless Docker the "
        "host socket lives at /run/user/$UID/docker.sock and privileged "
        "container launches will receive an empty directory at /run/docker.sock.",
        DOCKER_SOCK,
    )
    return DOCKER_SOCK


async def _resolve_host_socket_dir(
    docker: DockerClient, own_id: str | None
) -> str:
    """Return the host-side path of the per-container socket directory.

    Docker resolves bind-mount sources against the host filesystem, so
    when the orchestrator bind-mounts a socket file into a micro-container
    it must use the HOST path of the socket, not the in-container path
    (SOCKET_DIR).  We discover the right value by self-inspecting: look
    for the mount whose Destination is SOCKET_DIR and return its Source.
    Falls back to SOCKET_DIR, which is only correct when the host and
    container paths match.
    """
    if own_id is not None:
        try:
            info = await docker.inspect_container(own_id)
            for mount in info.get("Mounts", []) or []:
                if mount.get("Destination") == SOCKET_DIR:
                    source = mount.get("Source")
                    if source:
                        logger.info(
                            "Host socket directory from self-inspect: %s", source
                        )
                        return source
            logger.warning(
                "Self-inspect succeeded but no mount with destination %s "
                "was found; micro-container socket binds may fail.",
                SOCKET_DIR,
            )
        except Exception:
            logger.exception(
                "Self-inspect failed while resolving host socket directory"
            )
    logger.warning(
        "Falling back to %s as the host socket directory. This is only "
        "correct when the sockets directory is mounted at the same path "
        "on the host and inside the orchestrator container.",
        SOCKET_DIR,
    )
    return SOCKET_DIR


def _gvisor_has_host_uds(runtime_args: list) -> bool:
    """Return True if --host-uds is set to a value that allows connect().

    The required values are 'open' or 'all'.  'create' only allows bind(),
    not connect().  The default when the flag is absent is 'none' (blocks
    all host UDS access).
    """
    for arg in runtime_args:
        if isinstance(arg, str) and arg.startswith("--host-uds="):
            return arg.split("=", 1)[1] in ("open", "all")
    return False


async def _warn_if_gvisor_misconfigured(docker: DockerClient) -> None:
    """Log a warning if runsc is registered without the --host-uds flag.

    Non-privileged micro-containers run under gVisor (runsc).  Without
    ``--host-uds=all`` (or ``open``) in the runtime's args, gVisor blocks
    the guest agent from connecting to the bind-mounted orchestrator socket
    and every container times out with ``init_timeout``.  We detect this at
    startup so operators see the root cause immediately rather than chasing
    cryptic ``ConnectionRefusedError`` logs from inside the container.
    """
    try:
        info = await docker.get_info()
        runtimes = info.get("Runtimes") or {}
        if "runsc" not in runtimes:
            return
        runtime_args = runtimes["runsc"].get("Args") or []
        if _gvisor_has_host_uds(runtime_args):
            logger.info("gVisor (runsc) runtime detected with host-uds support")
        else:
            logger.warning(
                "gVisor (runsc) runtime is registered but '--host-uds=all' is absent "
                "from its runtimeArgs (current args: %s). Non-privileged containers "
                "will fail with ConnectionRefusedError when the guest agent tries to "
                "connect to the orchestrator socket. Add '--host-uds=all' to the runsc "
                "runtimeArgs in your Docker daemon config and restart Docker. "
                "See docs/install-runsc-gvisor.md for setup instructions.",
                runtime_args,
            )
    except Exception:
        logger.exception("Failed to check Docker runtime configuration")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    setup_logging(config.log_level)
    logger.info("Starting Drover orchestrator")
    if config.api_key_hash is None:
        logger.warning(
            "DROVER_API_KEY is not set — API authentication is disabled"
        )
    db = Database(config.db_path)
    await db.connect()
    docker = DockerClient(config)
    sockets = SocketManager(config, db)
    log_capture = LogCaptureManager(config, docker)

    await _warn_if_gvisor_misconfigured(docker)

    own_id = _detect_own_container_id()
    if own_id is None:
        try:
            cgroup_dump = Path("/proc/self/cgroup").read_text()
        except OSError as e:
            cgroup_dump = f"<unreadable: {e}>"
        try:
            mountinfo_dump = Path("/proc/self/mountinfo").read_text()
        except OSError as e:
            mountinfo_dump = f"<unreadable: {e}>"
        try:
            hostname_dump = socket.gethostname()
        except OSError as e:
            hostname_dump = f"<unreadable: {e}>"
        logger.warning(
            "Could not detect orchestrator's own Docker container ID; "
            "/logs/orchestrator endpoint will return 503. "
            "hostname=%r, /proc/self/cgroup=%r, /proc/self/mountinfo=%r",
            hostname_dump,
            cgroup_dump,
            mountinfo_dump,
        )
    else:
        logger.info("Detected orchestrator container ID: %s", own_id)

    host_docker_sock = await _resolve_host_docker_sock(docker, own_id)
    host_socket_dir = await _resolve_host_socket_dir(docker, own_id)

    container_manager = ContainerManager(
        config, db, docker, sockets, log_capture,
        host_docker_sock=host_docker_sock,
        host_socket_dir=host_socket_dir,
    )
    await container_manager.sync_containers()

    async def _handle_container_done(container_id: str) -> None:
        try:
            await container_manager.stop_container(container_id)
            logger.info("Container %s stopped via done signal", container_id)
        except (ContainerStateConflict, ContainerNotFound):
            pass
        except Exception:
            logger.exception(
                "Failed to stop container %s after done signal", container_id
            )

    sockets.set_done_callback(_handle_container_done)
    sockets.set_ready_callback(container_manager.on_container_ready)

    app.state.config = config
    app.state.db = db
    app.state.docker = docker
    app.state.sockets = sockets
    app.state.log_capture = log_capture
    app.state.container_manager = container_manager
    app.state.orchestrator_docker_id = own_id
    app.state.host_docker_sock = host_docker_sock

    reaper_task = asyncio.create_task(
        _reaper_loop(config, db, container_manager)
    )

    yield

    reaper_task.cancel()
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass

    await container_manager.shutdown()
    await log_capture.shutdown()
    await sockets.close_all()
    await docker.close()
    await db.close()


app = FastAPI(title="Drover Orchestrator", lifespan=lifespan)
app.include_router(images.router)
app.include_router(containers.router)


@app.exception_handler(httpx.RequestError)
async def _httpx_request_error(request: Request, exc: httpx.RequestError) -> JSONResponse:
    msg = str(exc)
    config: Config = request.app.state.config
    sock_path = config.docker_sock
    logger.error("Docker daemon unreachable (%s): %s", sock_path, msg)

    sock_hint = _docker_sock_hint(sock_path)
    if sock_hint is not None:
        detail = sock_hint
    elif "Permission denied" in msg:
        detail = (
            f"Permission denied accessing the Docker socket at {sock_path}. "
            "The orchestrator user is not in the socket's group — the entrypoint "
            "tries to join it automatically, so this usually means the socket "
            "wasn't a socket when the container started."
        )
    else:
        detail = f"Cannot reach the Docker daemon at {sock_path}: {msg}"
    return JSONResponse(status_code=503, content={"detail": detail})


def _docker_sock_hint(sock_path: str) -> str | None:
    """Return a human-readable hint when *sock_path* isn't a usable socket."""
    import os
    import stat

    try:
        st = os.stat(sock_path)
    except FileNotFoundError:
        return (
            f"The Docker socket at {sock_path} does not exist inside the "
            "container. Check that the bind-mount is configured."
        )
    except OSError as exc:
        return f"Cannot stat {sock_path}: {exc}"

    if stat.S_ISSOCK(st.st_mode):
        return None
    if stat.S_ISDIR(st.st_mode):
        return (
            f"{sock_path} is a directory inside the container, not a socket. "
            "Docker created it because the bind-mount source path does not "
            "exist on the host. For rootless Docker the socket lives at "
            "/run/user/$UID/docker.sock — bind that host path to "
            "/var/run/docker.sock in your compose file and recreate the stack."
        )
    return f"{sock_path} exists but is not a Unix socket (mode={oct(st.st_mode)})."


@app.exception_handler(DockerError)
async def _docker_error(request: Request, exc: DockerError) -> JSONResponse:
    logger.warning("Docker API error: %s", exc)
    return JSONResponse(
        status_code=502,
        content={"detail": f"Docker API error ({exc.status_code}): {exc.message}"},
    )


@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:
    method = request.method
    path = request.url.path
    logger.info("%s %s", method, path)
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info("%s %s -> %d (%.0fms)", method, path, response.status_code, duration_ms)
    return response


app.middleware("http")(auth_middleware)


@app.get("/health")
async def health(request: Request):
    config: Config = request.app.state.config
    return {
        "healthy": True,
        "privileged_image": config.privileged_image,
    }
