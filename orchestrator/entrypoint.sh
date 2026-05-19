#!/bin/sh
# Ensure the orchestrator user can access the mounted Docker socket, then drop
# privileges. The socket's group ID varies across hosts (rootful Docker uses
# the host's "docker" group; rootless Docker uses the invoking user's group),
# so we resolve it at runtime instead of baking a GID into the image.
set -e

SOCK="${DROVER_DOCKER_SOCK:-/var/run/docker.sock}"

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
	echo "entrypoint:   For rootless Docker, point the bind-mount at /run/user/\$UID/docker.sock" >&2
	echo "entrypoint:   (e.g. set DROVER_DOCKER_SOCK=\$XDG_RUNTIME_DIR/docker.sock before 'docker compose up')." >&2
elif [ ! -e "$SOCK" ]; then
	echo "entrypoint: warning: $SOCK does not exist; is the docker socket mounted into the container?" >&2
else
	echo "entrypoint: warning: $SOCK exists but is not a socket; check the bind-mount source path." >&2
fi

exec gosu orchestrator "$@"
