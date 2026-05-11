# syntax=docker/dockerfile:1
#
# Drover builder image.
#
# A privileged micro-container template: the operator points the
# orchestrator at this image via PRIVILEGED_IMAGE and it gets launched
# with the host Docker socket bind-mounted at /run/docker.sock, so
# commands sent over the orchestrator socket can build, pull, tag, and
# push images on the host daemon.
#
# Contents:
#   - python 3.12       : runs the guest agent (drover-executor)
#   - docker-ce-cli     : talks to the bind-mounted host Docker daemon
#   - docker-buildx     : modern image builds (`docker buildx build`)
#   - git               : clone source repos to build from
#   - drover-executor   : the reference guest agent from /executor
#
# See ./README.md for build, run, and customisation notes.

FROM python:3.12-slim

LABEL drover.managed="true"
LABEL drover.name="builder"

# Install build tooling and the Docker CLI from Docker's official apt
# repo. gnupg/curl are only needed to add that repo, so we drop them
# again at the end of the layer.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg; \
    install -m 0755 -d /etc/apt/keyrings; \
    curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg; \
    chmod a+r /etc/apt/keyrings/docker.gpg; \
    arch="$(dpkg --print-architecture)"; \
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"; \
    echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${codename} stable" \
        > /etc/apt/sources.list.d/docker.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        docker-ce-cli \
        docker-buildx-plugin; \
    apt-get purge -y --auto-remove gnupg; \
    rm -rf /var/lib/apt/lists/*

# Install the Drover executor (guest agent) directly from the repo. The
# build context is ./builder so we can't COPY ../executor in; the
# git+pip pattern from /executor/README.md is the supported install.
ARG DROVER_EXECUTOR_REF=main
RUN pip install --no-cache-dir \
    "git+https://github.com/saibotsivad/drover.git@${DROVER_EXECUTOR_REF}#subdirectory=executor"

# Default agent: connect to /run/orchestrator.sock and serve commands
# until the orchestrator stops the container or the idle timeout fires.
CMD ["drover-executor"]
