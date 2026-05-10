#!/bin/sh
# Ensure the orchestrator user can access the mounted Docker socket, then drop
# privileges. The socket's group ID varies across hosts (rootful Docker uses
# the host's "docker" group; rootless Docker uses the invoking user's group),
# so we resolve it at runtime instead of baking a GID into the image.
set -e

SOCK="${DOCKER_SOCK:-/var/run/docker.sock}"

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
else
	echo "entrypoint: warning: $SOCK is not a socket; is it mounted into the container?" >&2
fi

exec gosu orchestrator "$@"
