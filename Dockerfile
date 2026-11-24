FROM node:22-alpine AS frontend-builder

WORKDIR /frontend
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-alpine AS builder

WORKDIR /build
COPY requirements.txt ./
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels -r requirements.txt

FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/state

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels \
    && addgroup -g 1000 appuser \
    && adduser -D -u 1000 -G appuser appuser \
    && mkdir -p /data /state \
    && chown appuser:appuser /data /state

COPY api_server.py auth_manager.py event_hub.py folder_manager.py immich_manager.py script_gphoto.py security.py sync_service.py upload_state.py uploader_config.py ./
COPY --from=frontend-builder /frontend/dist ./frontend/dist

EXPOSE 8080

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/script_gphoto.py", "--healthcheck"]

ENTRYPOINT ["python", "-u", "/app/script_gphoto.py"]
