FROM python:3.12-slim AS builder

WORKDIR /build

COPY orchestrator/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# -------------------------------------------------------------------

FROM python:3.12-slim

# gosu lets the entrypoint start as root, adjust the docker.sock group, then
# drop privileges to the orchestrator user before exec'ing the server.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gosu && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Create non-root user and the fixed Drover paths.  /var/lib/drover/data
# holds the SQLite database (and future config); /var/lib/drover/logs is
# where DROVER_ENABLE_CONTAINER_LOGS=true writes per-container capture;
# /var/run/drover/sockets holds the per-micro-container Unix sockets.
RUN groupadd -g 1000 orchestrator && \
    useradd -u 1000 -g orchestrator -m orchestrator && \
    mkdir -p /var/lib/drover/data /var/lib/drover/logs /var/run/drover/sockets && \
    chown -R orchestrator:orchestrator /var/lib/drover /var/run/drover

WORKDIR /app
COPY orchestrator/ ./orchestrator/
RUN chmod +x /app/orchestrator/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/orchestrator/entrypoint.sh"]
CMD ["uvicorn", "orchestrator.app:app", "--host", "0.0.0.0", "--port", "8000"]
