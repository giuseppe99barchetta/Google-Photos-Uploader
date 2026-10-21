# Stage 1: Build dependencies (multi-stage build)
FROM python:3.11-alpine AS builder

WORKDIR /app

# Install system dependencies required for build
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libc-dev \
    linux-headers \
    cifs-utils

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Final image
FROM python:3.11-alpine

# Install only essential runtime dependencies
RUN apk add --no-cache \
    cifs-utils \
    inotify-tools

# Copy dependencies from the build stage into a location readable by appuser
COPY --from=builder /install /usr/local

# Configure non-root user
RUN adduser -D -u 1000 appuser && \
    mkdir /data /state && \
    chown appuser:appuser /data /state

WORKDIR /app

# Copy only necessary files
COPY script_gphoto.py upload_state.py uploader_config.py ./

USER appuser
ENV PYTHONUNBUFFERED=1 \
    WATCHED_FOLDER=/data \
    STATE_DIR=/state \
    HOME=/state

# Fail the build if the runtime user cannot import the application dependencies.
RUN python -c "from watchdog.observers.polling import PollingObserver; from watchdog.events import FileSystemEventHandler; from gpmc import Client"

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "script_gphoto.py", "--healthcheck"]

CMD ["python", "-u", "script_gphoto.py"]
