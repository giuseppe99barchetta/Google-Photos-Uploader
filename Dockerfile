# Stage 1: Build dependencies (multi-stage build)
FROM python:3.11-alpine AS builder

WORKDIR /app

# Install system dependencies required for build
RUN apk add --no-cache \
    gcc \
    musl-dev \
    linux-headers

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Final image
FROM python:3.11-alpine

# Copy dependencies from the build stage into a location readable by appuser
COPY --from=builder /install /usr/local

# Configure non-root user
RUN adduser -D -u 1000 appuser && \
    mkdir /data /state && \
    chown appuser:appuser /data /state && \
    chmod 700 /state

WORKDIR /app

# Copy only necessary files
COPY script_gphoto.py upload_state.py uploader_config.py ./

USER appuser
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    WATCHED_FOLDER=/data \
    STATE_DIR=/state \
    HOME=/state

# GPMC can write its cache and failure log here without elevated permissions.
WORKDIR /state

# Fail the build if the runtime user cannot import the application dependencies.
RUN python -m pip check && \
    python -c "import script_gphoto; from gpmc import Client"

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "/app/script_gphoto.py", "--healthcheck"]

CMD ["python", "-u", "/app/script_gphoto.py"]
