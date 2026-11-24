"""FastAPI REST API, Server-Sent Events stream, and static dashboard host."""

import asyncio
import csv
import io
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from security import CSRF_HEADER_NAME, SESSION_COOKIE_NAME, SESSION_TTL_SECONDS
from sync_service import SyncService

logger = logging.getLogger('gphotos_uploader')


# --- Pydantic Request Models ---


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SetupRequest(BaseModel):
    username: str = Field(default='admin', min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class FolderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=1024)
    enabled: bool = True
    recursive: bool = True
    extensions: list[str] | None = None
    exclude_dirs: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)
    min_file_size: int = Field(default=1, ge=1)
    max_file_size: int | None = Field(default=None, ge=1)
    account_id: int | None = None
    create_subfolder_name: str | None = Field(default=None, max_length=64)
    immich_preset: bool = False


class FolderUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    paused: bool | None = None
    recursive: bool | None = None
    extensions: list[str] | None = None
    exclude_dirs: list[str] | None = None
    exclude_patterns: list[str] | None = None
    min_file_size: int | None = Field(default=None, ge=1)
    max_file_size: int | None = Field(default=None, ge=1)
    account_id: int | None = None


class AccountCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    auth_data: str = Field(min_length=1, max_length=16384)
    create_dedicated_folder: bool = False
    parent_root: str | None = Field(default=None, max_length=1024)
    bind_folder_path: str | None = Field(default=None, max_length=1024)
    folder_name: str | None = Field(default=None, max_length=120)
    immich_preset: bool = False


class AccountRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ImmichConfigRequest(BaseModel):
    url: str | None = Field(default=None, max_length=512)
    public_url: str | None = Field(default=None, max_length=512)
    media_root: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=512)
    clear_api_key: bool = False


class ImmichCreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=160)
    password: str = Field(min_length=6, max_length=128)
    storage_label: str | None = Field(default=None, max_length=64)
    is_admin: bool = False
    account_id: int | None = None
    auto_bind: bool = True


class ImmichBindRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    account_id: int
    folder_name: str = Field(min_length=1, max_length=120)


class JobPathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2048)


class RetryAllRequest(BaseModel):
    folder_id: int | None = None


class AuthDataRequest(BaseModel):
    auth_data: str = Field(min_length=1, max_length=16384)


class SettingsUpdateRequest(BaseModel):
    upload_threads: int | None = Field(default=None, ge=1, le=8)
    upload_batch_size: int | None = Field(default=None, ge=1, le=64)
    scan_interval: float | None = Field(default=None, ge=5, le=86400)
    stable_seconds: float | None = Field(default=None, ge=0.01, le=300)
    stability_timeout: float | None = Field(default=None, ge=1, le=3600)
    default_recursive: bool | None = None
    upload_quality: Literal['original', 'saver'] | None = None
    use_quota: bool | None = None
    duplicate_detection: bool | None = None
    filename_conflict_policy: Literal['hash_dedup', 'skip_existing_filename'] | None = None
    delete_after_upload: bool | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=50)
    retry_base: float | None = Field(default=None, ge=1, le=3600)
    retry_max: float | None = Field(default=None, ge=5, le=86400)
    auto_retry_failed: bool | None = None
    notify_upload_completed: bool | None = None
    notify_upload_failed: bool | None = None
    notify_auth_issues: bool | None = None
    notify_folder_unavailable: bool | None = None
    notify_low_disk: bool | None = None
    notify_watcher_stopped: bool | None = None
    low_disk_threshold_gb: float | None = Field(default=None, ge=0.1, le=500)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


def _format_sse(event: dict[str, Any]) -> str:
    event_type = event.get('type', 'message')
    payload = json.dumps(event, separators=(',', ':'))
    return f'event: {event_type}\ndata: {payload}\n\n'


def create_app(
    service: SyncService,
    *,
    manage_lifecycle: bool = False,
    static_dir: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        if manage_lifecycle:
            service.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                service.stop()

    app = FastAPI(
        title='Google Photos Sync Manager API',
        version='2.0.0',
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware('http')
    async def add_security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def require_auth(request: Request) -> dict[str, Any]:
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        session = service.dashboard_auth.get_session(cookie)
        if session is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Authentication required')
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            csrf_token = request.headers.get(CSRF_HEADER_NAME)
            if not service.dashboard_auth.validate_csrf(cookie, csrf_token):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Invalid or missing CSRF token')
        return session

    def set_session_cookie(response: Response, cookie_value: str) -> None:
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=cookie_value,
            httponly=True,
            samesite='strict',
            secure=service.dashboard_auth.secure_cookies,
            max_age=SESSION_TTL_SECONDS,
            path='/',
        )

    # --- Liveness (unauthenticated minimal probe for Docker) ---

    @app.get('/api/health/ping')
    def health_ping() -> dict[str, Any]:
        db_ok = service.store.db_stats()['ok']
        return {'ok': db_ok}

    # --- Dashboard Session / Authentication ---

    @app.get('/api/session/status')
    def session_status(request: Request) -> dict[str, Any]:
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        session = service.dashboard_auth.get_session(cookie)
        return {
            'authenticated': session is not None,
            'setup_required': service.dashboard_auth.setup_required,
            'username': session['username'] if session else None,
            'csrf_token': session['csrf_token'] if session else None,
        }

    @app.post('/api/session/setup')
    def session_setup(body: SetupRequest, request: Request, response: Response) -> dict[str, Any]:
        allowed, retry_after = service.dashboard_auth.login_limiter.check_and_record(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many attempts; try again later',
                headers={'Retry-After': str(retry_after)},
            )
        try:
            session = service.dashboard_auth.complete_setup(body.username, body.password)
        except PermissionError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None
        set_session_cookie(response, session['cookie_value'])
        service.store.record_activity('info', 'auth', 'dashboard_setup', 'Initial dashboard account configured')
        return {
            'authenticated': True,
            'username': session['username'],
            'csrf_token': session['csrf_token'],
        }

    @app.post('/api/session/login')
    def session_login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        allowed, retry_after = service.dashboard_auth.login_limiter.check_and_record(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many login attempts; try again later',
                headers={'Retry-After': str(retry_after)},
            )
        if service.dashboard_auth.setup_required:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Initial dashboard setup is required')
        session = service.dashboard_auth.authenticate(body.username, body.password)
        if session is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid username or password')
        set_session_cookie(response, session['cookie_value'])
        return {
            'authenticated': True,
            'username': session['username'],
            'csrf_token': session['csrf_token'],
        }

    @app.post('/api/session/logout')
    def session_logout(request: Request, response: Response, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        service.dashboard_auth.logout(request.cookies.get(SESSION_COOKIE_NAME))
        response.delete_cookie(key=SESSION_COOKIE_NAME, path='/')
        return {'authenticated': False}

    @app.post('/api/session/password')
    def change_password(body: ChangePasswordRequest, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        try:
            changed = service.dashboard_auth.change_password(body.current_password, body.new_password)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None
        if not changed:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Current password is incorrect')
        service.store.record_activity('info', 'auth', 'dashboard_password_changed', 'Dashboard password updated')
        return {'updated': True}

    # --- Overview ---

    @app.get('/api/overview')
    def get_overview(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return service.get_overview()

    # --- Monitored Folders ---

    @app.get('/api/folders')
    def list_folders(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return {
            'folders': service.store.list_folders(),
            'approved_roots': [str(r) for r in service.approved_roots],
            'accounts': service.list_accounts(),
        }

    @app.get('/api/folders/browse')
    def browse_folders(
        path: str | None = Query(default=None),
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return service.browse_directories(path)
        except PermissionError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None

    @app.post('/api/folders', status_code=status.HTTP_201_CREATED)
    def create_folder(body: FolderCreateRequest, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        try:
            folder = service.add_folder(
                name=body.name,
                path=body.path,
                enabled=body.enabled,
                recursive=body.recursive,
                extensions=body.extensions,
                exclude_dirs=body.exclude_dirs,
                exclude_patterns=body.exclude_patterns,
                min_file_size=body.min_file_size,
                max_file_size=body.max_file_size,
                account_id=body.account_id,
                create_subfolder_name=body.create_subfolder_name,
                immich_preset=body.immich_preset,
            )
            return {'folder': folder}
        except PermissionError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None

    @app.patch('/api/folders/{folder_id}')
    def patch_folder(
        folder_id: int,
        body: FolderUpdateRequest,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            updates = body.model_dump(exclude_unset=True)
            folder = service.update_folder(folder_id, updates)
            return {'folder': folder}
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Folder not found') from None
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None

    @app.delete('/api/folders/{folder_id}')
    def delete_folder(folder_id: int, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        try:
            service.remove_folder(folder_id)
            return {'removed': True}
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Folder not found') from None

    @app.post('/api/folders/{folder_id}/pause')
    def pause_folder(folder_id: int, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        try:
            return {'folder': service.update_folder(folder_id, {'paused': True})}
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Folder not found') from None

    @app.post('/api/folders/{folder_id}/resume')
    def resume_folder(folder_id: int, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        try:
            return {'folder': service.update_folder(folder_id, {'paused': False})}
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Folder not found') from None

    @app.post('/api/folders/{folder_id}/scan')
    def scan_folder(folder_id: int, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        try:
            return service.scan_folder_now(folder_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Folder not found') from None
        except PermissionError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from None
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None

    # --- Upload Queue ---

    @app.get('/api/queue')
    def list_queue(
        search: str = Query(default=''),
        folder_id: int | None = Query(default=None),
        account_id: int | None = Query(default=None),
        file_type: str = Query(default=''),
        status_filter: str = Query(default='', alias='status'),
        sort_by: str = Query(default='created_at'),
        sort_dir: str = Query(default='desc'),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        items, total = service.store.list_jobs(
            search=search,
            folder_id=folder_id,
            account_id=account_id,
            file_type=file_type,
            status=status_filter,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
        live = service.events.live_snapshot()
        active_by_path = {item['path']: item for item in live['active_uploads']}
        enriched = []
        for job in items:
            active_info = active_by_path.get(job['path'])
            enriched.append({
                **job,
                'filename': Path(job['path']).name,
                'live': active_info,
            })
        return {
            'items': enriched,
            'total': total,
            'limit': limit,
            'offset': offset,
            'queue_paused': service.is_queue_paused(),
            'stats': service.store.queue_stats(),
            'live': live,
        }

    @app.get('/api/queue/job')
    def inspect_job(path: str = Query(min_length=1), _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        job = service.store.get(path)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
        return {'job': {**job, 'filename': Path(job['path']).name}}

    @app.post('/api/queue/pause')
    def pause_queue(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return service.pause_queue()

    @app.post('/api/queue/resume')
    def resume_queue(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return service.resume_queue()

    @app.post('/api/queue/retry')
    def retry_job(body: JobPathRequest, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        if not service.retry_job(body.path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Eligible job not found')
        return {'retried': True}

    @app.post('/api/queue/retry-all')
    def retry_all_jobs(body: RetryAllRequest, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        count = service.retry_all_failed(folder_id=body.folder_id)
        return {'retried_count': count}

    @app.post('/api/queue/cancel')
    def cancel_job(body: JobPathRequest, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        if not service.cancel_job(body.path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Pending cancellable job not found')
        return {'cancelled': True}

    # --- Live SSE Stream ---

    @app.get('/api/events')
    async def event_stream(request: Request, _session: dict[str, Any] = Depends(require_auth)) -> StreamingResponse:
        loop = asyncio.get_running_loop()
        queue = service.events.subscribe(loop)

        async def generator() -> AsyncGenerator[str, None]:
            try:
                initial = {
                    'type': 'snapshot',
                    'data': {
                        'live': service.events.live_snapshot(),
                        'stats': service.store.queue_stats(),
                        'queue_paused': service.is_queue_paused(),
                    },
                }
                yield _format_sse(initial)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield _format_sse(event)
                    except TimeoutError:
                        yield ': keepalive\n\n'
            finally:
                service.events.unsubscribe(loop, queue)

        return StreamingResponse(
            generator(),
            media_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
            },
        )

    # --- Upload History & Export ---

    @app.get('/api/history')
    def list_history(
        search: str = Query(default=''),
        outcome: str = Query(default=''),
        folder_id: int | None = Query(default=None),
        account_id: int | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        items, total = service.store.list_history(
            search=search, outcome=outcome, folder_id=folder_id, account_id=account_id, limit=limit, offset=offset
        )
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    @app.get('/api/history/export')
    def export_history(
        export_format: Literal['csv', 'json'] = Query(default='csv', alias='format'),
        outcome: str = Query(default=''),
        folder_id: int | None = Query(default=None),
        account_id: int | None = Query(default=None),
        _session: dict[str, Any] = Depends(require_auth),
    ) -> Response:
        items, _ = service.store.list_history(
            outcome=outcome, folder_id=folder_id, account_id=account_id, limit=5000, offset=0
        )
        if export_format == 'json':
            return JSONResponse(
                content={'items': items},
                headers={'Content-Disposition': 'attachment; filename="upload-history.json"'},
            )
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=[
                'id', 'filename', 'folder_name', 'account_name', 'file_size', 'file_ext',
                'outcome', 'media_key', 'duration', 'error', 'created_at',
            ],
            extrasaction='ignore',
        )
        writer.writeheader()
        for row in items:
            writer.writerow(row)
        return Response(
            content=buffer.getvalue(),
            media_type='text/csv; charset=utf-8',
            headers={'Content-Disposition': 'attachment; filename="upload-history.csv"'},
        )

    # --- Activity Logs & Export ---

    @app.get('/api/logs')
    def list_logs(
        search: str = Query(default=''),
        level: str = Query(default=''),
        category: str = Query(default=''),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        items, total = service.store.list_activity(
            search=search, level=level, category=category, limit=limit, offset=offset
        )
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    @app.get('/api/logs/export')
    def export_logs(
        export_format: Literal['json', 'csv'] = Query(default='json', alias='format'),
        level: str = Query(default=''),
        category: str = Query(default=''),
        _session: dict[str, Any] = Depends(require_auth),
    ) -> Response:
        items, _ = service.store.list_activity(level=level, category=category, limit=5000, offset=0)
        if export_format == 'json':
            return JSONResponse(
                content={'items': items},
                headers={'Content-Disposition': 'attachment; filename="activity-logs.json"'},
            )
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=['id', 'level', 'category', 'event_type', 'message', 'created_at'],
            extrasaction='ignore',
        )
        writer.writeheader()
        for row in items:
            writer.writerow(row)
        return Response(
            content=buffer.getvalue(),
            media_type='text/csv; charset=utf-8',
            headers={'Content-Disposition': 'attachment; filename="activity-logs.csv"'},
        )

    # --- Immich Integration ---

    @app.get('/api/immich/status')
    def get_immich_status(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return service.get_immich_status()

    @app.put('/api/immich/config')
    def update_immich_config(
        body: ImmichConfigRequest,
        request: Request,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        if body.api_key:
            allowed, retry_after = service.dashboard_auth.secret_limiter.check_and_record(_client_ip(request))
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail='Too many credential updates; wait before retrying',
                    headers={'Retry-After': str(retry_after)},
                )
        try:
            return service.configure_immich(
                url=body.url,
                public_url=body.public_url,
                media_root=body.media_root,
                api_key=body.api_key,
                clear_api_key=body.clear_api_key,
            )
        except PermissionError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None

    @app.get('/api/immich/users')
    def list_immich_users(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        try:
            return service.list_immich_users()
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from None

    @app.post('/api/immich/users', status_code=status.HTTP_201_CREATED)
    def create_immich_user(
        body: ImmichCreateUserRequest,
        request: Request,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        allowed, retry_after = service.dashboard_auth.secret_limiter.check_and_record(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many requests; wait before retrying',
                headers={'Retry-After': str(retry_after)},
            )
        try:
            return service.create_immich_user(
                name=body.name,
                email=body.email,
                password=body.password,
                storage_label=body.storage_label,
                is_admin=body.is_admin,
                account_id=body.account_id,
                auto_bind=body.auto_bind,
            )
        except PermissionError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None

    @app.post('/api/immich/bind')
    def bind_immich_folder(
        body: ImmichBindRequest,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return service.bind_immich_folder(
                path=body.path,
                account_id=body.account_id,
                folder_name=body.folder_name,
            )
        except PermissionError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None

    # --- Multi-Account Management (Protected & Rate-Limited) ---

    @app.get('/api/accounts')
    def list_accounts(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return {
            'accounts': service.list_accounts(),
            'approved_roots': service.browse_directories(None)['approved_roots'],
        }

    @app.post('/api/accounts/validate')
    def validate_account_credentials(
        body: AuthDataRequest,
        request: Request,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        allowed, retry_after = service.dashboard_auth.secret_limiter.check_and_record(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many authentication validation attempts; wait before retrying',
                headers={'Retry-After': str(retry_after)},
            )
        return service.validate_auth(body.auth_data)

    @app.post('/api/accounts', status_code=status.HTTP_201_CREATED)
    def create_account(
        body: AccountCreateRequest,
        request: Request,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        allowed, retry_after = service.dashboard_auth.secret_limiter.check_and_record(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many account connection attempts; wait before retrying',
                headers={'Retry-After': str(retry_after)},
            )
        try:
            result = service.add_account(
                name=body.name,
                auth_data=body.auth_data,
                create_dedicated_folder=body.create_dedicated_folder,
                parent_root=body.parent_root,
                bind_folder_path=body.bind_folder_path,
                folder_name=body.folder_name,
                immich_preset=body.immich_preset,
            )
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None
        if not result.get('saved'):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result['message'])
        return result

    @app.patch('/api/accounts/{account_id}')
    def rename_account(
        account_id: int,
        body: AccountRenameRequest,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return {'account': service.rename_account(account_id, body.name)}
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Account not found') from None
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None

    @app.post('/api/accounts/{account_id}/credentials')
    def rotate_account_credentials(
        account_id: int,
        body: AuthDataRequest,
        request: Request,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        allowed, retry_after = service.dashboard_auth.secret_limiter.check_and_record(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many credential rotation attempts; wait before retrying',
                headers={'Retry-After': str(retry_after)},
            )
        try:
            result = service.rotate_account_credentials(account_id, body.auth_data)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Account not found') from None
        if not result.get('saved'):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result['message'])
        return result

    @app.post('/api/accounts/{account_id}/revalidate')
    def revalidate_account_endpoint(
        account_id: int,
        request: Request,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        allowed, retry_after = service.dashboard_auth.secret_limiter.check_and_record(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many revalidation requests; wait before retrying',
                headers={'Retry-After': str(retry_after)},
            )
        try:
            return service.revalidate_account(account_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Account not found') from None

    @app.delete('/api/accounts/{account_id}')
    def delete_account(
        account_id: int,
        force: bool = Query(default=False),
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return service.remove_account(account_id, force=force)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Account not found') from None
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None

    # --- Android Authentication Management (Legacy Primary-Account Endpoints) ---

    @app.get('/api/auth/status')
    def get_auth_status(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return service.auth.status()

    @app.post('/api/auth/validate')
    def validate_auth_endpoint(
        body: AuthDataRequest,
        request: Request,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        allowed, retry_after = service.dashboard_auth.secret_limiter.check_and_record(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many authentication validation attempts; wait before retrying',
                headers={'Retry-After': str(retry_after)},
            )
        return service.validate_auth(body.auth_data)

    @app.post('/api/auth/save')
    def save_auth_endpoint(
        body: AuthDataRequest,
        request: Request,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        allowed, retry_after = service.dashboard_auth.secret_limiter.check_and_record(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many authentication update attempts; wait before retrying',
                headers={'Retry-After': str(retry_after)},
            )
        result = service.save_auth(body.auth_data)
        if not result.get('saved'):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result['message'])
        return result

    @app.post('/api/auth/revalidate')
    def revalidate_auth_endpoint(
        request: Request,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        allowed, retry_after = service.dashboard_auth.secret_limiter.check_and_record(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many revalidation requests; wait before retrying',
                headers={'Retry-After': str(retry_after)},
            )
        return service.revalidate_auth()

    # --- Settings ---

    @app.get('/api/settings')
    def get_settings(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return {
            'settings': service.get_settings(),
            'watcher': service.watchers.watcher_summary(),
        }

    @app.put('/api/settings')
    def update_settings(
        body: SettingsUpdateRequest,
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        patch = body.model_dump(exclude_unset=True)
        try:
            updated = service.update_settings(patch)
            return {'settings': updated}
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None

    # --- Notifications ---

    @app.get('/api/notifications')
    def get_notifications(
        include_resolved: bool = Query(default=False),
        _session: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        items, unread = service.store.list_notifications(include_resolved=include_resolved)
        return {'items': items, 'unread_count': unread}

    @app.post('/api/notifications/{notification_id}/read')
    def mark_read(notification_id: int, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return {'ok': service.store.mark_notification_read(notification_id)}

    @app.post('/api/notifications/read-all')
    def mark_all_read(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return {'updated': service.store.mark_all_notifications_read()}

    @app.post('/api/notifications/{notification_id}/dismiss')
    def dismiss_notification(notification_id: int, _session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return {'ok': service.store.dismiss_notification(notification_id)}

    # --- System Health ---

    @app.get('/api/health')
    def get_health(_session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return service.get_system_health()

    # --- Built React SPA Static Serving ---

    dist_dir = static_dir or (Path(__file__).resolve().parent / 'frontend' / 'dist')
    index_file = dist_dir / 'index.html'
    assets_dir = dist_dir / 'assets'
    if assets_dir.is_dir():
        app.mount('/assets', StaticFiles(directory=str(assets_dir)), name='assets')

    if index_file.is_file():
        @app.get('/{full_path:path}')
        def serve_spa(full_path: str) -> Response:
            if full_path.startswith('api/'):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')
            candidate = (dist_dir / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(dist_dir.resolve()):
                return FileResponse(str(candidate))
            return FileResponse(str(index_file))

    return app


def run_dashboard_server(config: Any) -> int:
    import uvicorn

    from script_gphoto import ShutdownTimeout

    service = SyncService(config)
    app = create_app(service, manage_lifecycle=True)
    server_config = uvicorn.Config(
        app,
        host=config.dashboard_host,
        port=config.dashboard_port,
        log_level=config.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(server_config)
    try:
        server.run()
        return 0
    except ShutdownTimeout as error:
        logger.error('%s', error)
        return 1
