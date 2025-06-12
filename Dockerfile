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
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Final image
FROM python:3.11-alpine

# Install only essential runtime dependencies
RUN apk add --no-cache \
    cifs-utils \
    inotify-tools

# Copy dependencies from the build stage
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Configure non-root user
RUN adduser -D -u 1000 appuser && \
    mkdir /data && \
    chown appuser:appuser /data

WORKDIR /app

# Copy only necessary files
COPY script_gphoto.py .

USER appuser
ENV PYTHONUNBUFFERED=1 \
    WATCHED_FOLDER=/data

CMD ["python", "-u", "script_gphoto.py"]
