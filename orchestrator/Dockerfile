FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# -------------------------------------------------------------------

FROM python:3.12-slim

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN groupadd -g 1000 orchestrator && \
    useradd -u 1000 -g orchestrator -m orchestrator && \
    mkdir -p /var/lib/orchestrator /var/run/microcontainers && \
    chown orchestrator:orchestrator /var/lib/orchestrator /var/run/microcontainers

WORKDIR /app
COPY . .

USER 1000

EXPOSE 8000

CMD ["uvicorn", "orchestrator.app:app", "--host", "0.0.0.0", "--port", "8000"]
