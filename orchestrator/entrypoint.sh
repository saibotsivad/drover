#!/bin/sh
# Runs as root just long enough to fix up bind-mounted paths whose
# host-side ownership we can't predict, then drops to UID 1000 via gosu.
#
# 1. The Docker socket's GID varies across hosts (rootful Docker uses the
#    host's "docker" group; rootless Docker uses the invoking user's
#    group), so we resolve it at runtime instead of baking a GID into the
#    image.
# 2. The data, logs, and per-micro-container socket directories are
#    bind-mounted from the host. Docker creates a missing host directory
#    as root:root mode 0755 by default, which UID 1000 can't write to.
#    We chown all three on every start so the operator never has to
#    pre-create or chown the host directories.
#
# The host Docker socket path inside the container is fixed at
# /var/run/docker.sock; the sockets directory is fixed at
# /var/run/drover/sockets. Both match the orchestrator's hardcoded
# defaults in orchestrator/config.py.
set -e

SOCK=/var/run/docker.sock
DATA_DIR=/var/lib/drover/data
LOG_DIR=/var/lib/drover/logs
SOCKET_DIR=/var/run/drover/sockets

if [ -S "$SOCK" ]; then
	SOCK_GID=$(stat -c '%g' "$SOCK")
	if [ "$SOCK_GID" -eq 0 ]; then
		echo "entrypoint: warning: $SOCK is owned by group root; orchestrator user may not have access" >&2
	else
		GROUP_NAME=$(getent group "$SOCK_GID" 2>/dev/null | cut -d: -f1 || true)
		if [ -z "$GROUP_NAME" ]; then
			GROUP_NAME=dockersock
			groupadd -g "$SOCK_GID" "$GROUP_NAME"
		fi
		if ! id -nG orchestrator | tr ' ' '\n' | grep -qx "$GROUP_NAME"; then
			usermod -aG "$GROUP_NAME" orchestrator
		fi
	fi
elif [ -d "$SOCK" ]; then
	# Docker bind-mounts a missing host path as an empty directory inside the
	# container. This usually means the host path doesn't exist — most common
	# cause is rootless Docker, where the socket lives at
	# /run/user/$UID/docker.sock, not /var/run/docker.sock.
	echo "entrypoint: warning: $SOCK is a directory, not a socket. The host path probably doesn't exist." >&2
	echo "entrypoint:   For rootless Docker, point the host-side bind-mount at /run/user/\$UID/docker.sock." >&2
elif [ ! -e "$SOCK" ]; then
	echo "entrypoint: warning: $SOCK does not exist; is the docker socket mounted into the container?" >&2
else
	echo "entrypoint: warning: $SOCK exists but is not a socket; check the bind-mount source path." >&2
fi

# Ensure UID 1000 owns the bind-mounted directories regardless of how the
# host directories were created.  Docker creates a missing host bind-mount
# source as root:root 0755, which UID 1000 can't write to.  Chowning here
# is idempotent and cheap.
mkdir -p "$DATA_DIR" "$LOG_DIR" "$SOCKET_DIR"
chown orchestrator:orchestrator "$DATA_DIR" "$LOG_DIR" "$SOCKET_DIR"

exec gosu orchestrator "$@"
