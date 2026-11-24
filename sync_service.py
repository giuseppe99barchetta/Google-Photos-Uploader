"""Controlled single-process service layer unifying the uploader, folders, auth, and API."""

import logging
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any

from auth_manager import AuthManager
from event_hub import EventHub
from folder_manager import (
    IMMICH_EXCLUDE_DIRS,
    IMMICH_EXCLUDE_PATTERNS,
    FolderWatcherPool,
    browse_approved_directories,
    create_dedicated_subfolder,
    normalize_extensions,
    normalize_string_list,
    validate_approved_path,
)
from immich_manager import ImmichManager
from script_gphoto import (
    MEDIA_EXTENSIONS,
    FileProcessor,
    ShutdownTimeout,
    write_health,
)
from security import DashboardAuthManager
from upload_state import StateStore
from uploader_config import (
    APP_VERSION,
    MAX_UPLOAD_BATCH_SIZE,
    MAX_UPLOAD_THREADS,
    MIN_UPLOAD_BATCH_SIZE,
    MIN_UPLOAD_THREADS,
    Config,
)

logger = logging.getLogger('gphotos_uploader')


def _default_client_factory(**kwargs: Any) -> Any:
    from gpmc import Client

    return Client(**kwargs)


class SyncService:
    """Single owner of SQLite queue state, watchers, upload worker, settings, and live events."""

    def __init__(
        self,
        config: Config,
        client_factory: Callable[..., Any] | None = None,
        auth_validator: Callable[[str, int], dict[str, Any]] | None = None,
        stop_event: Event | None = None,
        immich_http_request: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.approved_roots: tuple[Path, ...] = config.approved_roots or (config.root,)
        self.stop_event = stop_event or Event()
        self.started_at = time.time()
        self._lock = RLock()
        self._queue_paused = False
        self._last_scan_at: float | None = None
        self._started = False

        self.store = StateStore(config.state_dir / 'uploads.db', scope=config.state_scope)
        self.events = EventHub()
        self.auth = AuthManager(
            config,
            client_factory=client_factory or _default_client_factory,
            validator=auth_validator,
            store=self.store,
        )
        self.immich = ImmichManager(
            config,
            approved_roots=self.approved_roots,
            http_request=immich_http_request,
        )
        self.dashboard_auth = DashboardAuthManager(
            state_dir=config.state_dir,
            username=config.dashboard_username,
            initial_password=config.dashboard_password,
            session_secret=config.dashboard_session_secret,
            secure_cookies=config.dashboard_secure_cookies,
        )

        self._settings = self._load_settings()
        self._bootstrap_default_folder()

        self.processor = FileProcessor(
            client=None,
            store=self.store,
            delete_after_upload=bool(self._settings['delete_after_upload']),
            stop_event=self.stop_event,
            allowed_roots=self.approved_roots,
            stable_seconds=float(self._settings['stable_seconds']),
            stability_timeout=float(self._settings['stability_timeout']),
            max_attempts=int(self._settings['max_attempts']),
            retry_base=float(self._settings['retry_base']),
            retry_max=float(self._settings['retry_max']),
            upload_threads=int(self._settings['upload_threads']),
            upload_batch_size=int(self._settings['upload_batch_size']),
            settings_provider=self.get_settings,
            account_client_provider=self.auth.get_client,
            eligible_accounts_provider=self.auth.eligible_account_ids,
            default_account_provider=self.auth.get_primary_account_id,
            is_paused=self.is_queue_paused,
            on_progress=self._on_progress,
            on_batch_start=self._on_batch_start,
            on_batch_complete=self._on_batch_complete,
            on_file_confirmed=self._on_file_confirmed,
            on_file_failed=self._on_file_failed,
        )
        self.watchers = FolderWatcherPool(
            submit_callback=self._submit_from_folder,
            poll_interval=config.poll_interval,
            on_detected=self._on_file_detected,
        )
        self._worker_thread: Thread | None = None
        self._supervisor_thread: Thread | None = None
        self._next_scan_at = 0.0
        self._wake_supervisor = Event()

    # --- Settings ---

    def _default_settings(self) -> dict[str, Any]:
        return {
            'upload_threads': self.config.upload_threads,
            'upload_batch_size': self.config.upload_batch_size,
            'scan_interval': float(self.config.scan_interval),
            'stable_seconds': float(self.config.stable_seconds),
            'stability_timeout': float(self.config.stability_timeout),
            'default_recursive': True,
            'upload_quality': self.config.upload_quality,
            'use_quota': self.config.use_quota,
            'duplicate_detection': self.config.duplicate_detection,
            'filename_conflict_policy': self.config.filename_conflict_policy,
            'delete_after_upload': self.config.delete_after_upload,
            'max_attempts': self.config.max_attempts,
            'retry_base': float(self.config.retry_base),
            'retry_max': float(self.config.retry_max),
            'auto_retry_failed': self.config.auto_retry_failed,
            'notify_upload_completed': False,
            'notify_upload_failed': True,
            'notify_auth_issues': True,
            'notify_folder_unavailable': True,
            'notify_low_disk': True,
            'notify_watcher_stopped': True,
            'low_disk_threshold_gb': 5.0,
        }

    def _load_settings(self) -> dict[str, Any]:
        defaults = self._default_settings()
        saved = self.store.get_settings()
        merged = {**defaults}
        for key, value in saved.items():
            if key in defaults:
                merged[key] = value
        return merged

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def update_settings(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            candidate = dict(self._settings)
            for key, value in patch.items():
                if key not in candidate:
                    raise ValueError(f'Unknown setting: {key}')
                candidate[key] = value

            # Validate all fields strictly server-side.
            threads = int(candidate['upload_threads'])
            if isinstance(candidate['upload_threads'], bool) or not MIN_UPLOAD_THREADS <= threads <= MAX_UPLOAD_THREADS:
                raise ValueError(f'upload_threads must be between {MIN_UPLOAD_THREADS} and {MAX_UPLOAD_THREADS}')
            candidate['upload_threads'] = threads

            batch_size = int(candidate['upload_batch_size'])
            if isinstance(candidate['upload_batch_size'], bool) or not MIN_UPLOAD_BATCH_SIZE <= batch_size <= MAX_UPLOAD_BATCH_SIZE:
                raise ValueError(f'upload_batch_size must be between {MIN_UPLOAD_BATCH_SIZE} and {MAX_UPLOAD_BATCH_SIZE}')
            candidate['upload_batch_size'] = batch_size

            scan_interval = float(candidate['scan_interval'])
            if not 5 <= scan_interval <= 86400:
                raise ValueError('scan_interval must be between 5 and 86400 seconds')
            candidate['scan_interval'] = scan_interval

            stable_seconds = float(candidate['stable_seconds'])
            if not 0.01 <= stable_seconds <= 300:
                raise ValueError('stable_seconds must be between 0.01 and 300 seconds')
            candidate['stable_seconds'] = stable_seconds

            stability_timeout = float(candidate['stability_timeout'])
            if not 1 <= stability_timeout <= 3600:
                raise ValueError('stability_timeout must be between 1 and 3600 seconds')
            if stability_timeout < stable_seconds:
                raise ValueError('stability_timeout must be at least stable_seconds')
            candidate['stability_timeout'] = stability_timeout

            if candidate['upload_quality'] not in ('original', 'saver'):
                raise ValueError('upload_quality must be original or saver')
            if candidate['filename_conflict_policy'] not in ('hash_dedup', 'skip_existing_filename'):
                raise ValueError('filename_conflict_policy must be hash_dedup or skip_existing_filename')

            max_attempts = int(candidate['max_attempts'])
            if isinstance(candidate['max_attempts'], bool) or not 1 <= max_attempts <= 50:
                raise ValueError('max_attempts must be between 1 and 50')
            candidate['max_attempts'] = max_attempts

            retry_base = float(candidate['retry_base'])
            if not 1 <= retry_base <= 3600:
                raise ValueError('retry_base must be between 1 and 3600 seconds')
            candidate['retry_base'] = retry_base

            retry_max = float(candidate['retry_max'])
            if not 5 <= retry_max <= 86400:
                raise ValueError('retry_max must be between 5 and 86400 seconds')
            if retry_max < retry_base:
                raise ValueError('retry_max must be at least retry_base')
            candidate['retry_max'] = retry_max

            low_disk = float(candidate['low_disk_threshold_gb'])
            if not 0.1 <= low_disk <= 500:
                raise ValueError('low_disk_threshold_gb must be between 0.1 and 500 GB')
            candidate['low_disk_threshold_gb'] = low_disk

            for bool_field in (
                'default_recursive', 'use_quota', 'duplicate_detection',
                'delete_after_upload', 'auto_retry_failed', 'notify_upload_completed',
                'notify_upload_failed', 'notify_auth_issues', 'notify_folder_unavailable',
                'notify_low_disk', 'notify_watcher_stopped',
            ):
                candidate[bool_field] = bool(candidate[bool_field])

            changed_keys = [k for k in candidate if candidate[k] != self._settings[k]]
            self._settings = candidate
            self.store.set_settings(candidate)

        if changed_keys:
            self.store.record_activity(
                'info', 'settings', 'settings_updated',
                f'Updated settings: {", ".join(changed_keys)}',
                details={'changed': changed_keys},
            )
            self.events.publish('settings_updated', {'settings': self.get_settings()})
        return self.get_settings()

    def _bootstrap_default_folder(self) -> None:
        if self.store.list_folders():
            return
        if self.config.root.is_dir():
            self.store.add_folder(
                name=self.config.root.name or 'Primary Media',
                path=self.config.root,
                enabled=True,
                paused=False,
                recursive=bool(self._settings.get('default_recursive', True)),
                extensions=MEDIA_EXTENSIONS,
                account_id=self.auth.get_primary_account_id(),
            )

    # --- Lifecycle ---

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        write_health(self.config.state_dir, False)
        self._worker_thread = Thread(target=self.processor.run, name='upload-worker', daemon=True)
        self._supervisor_thread = Thread(target=self._supervisor_loop, name='folder-supervisor', daemon=True)
        self._worker_thread.start()
        self._supervisor_thread.start()
        self.store.record_activity(
            'info', 'system', 'service_started',
            f'Sync Manager started (threads={self._settings["upload_threads"]}, batch={self._settings["upload_batch_size"]})',
        )
        if not self.auth.status()['configured']:
            self._emit_notification(
                'auth:not_configured', 'warning', 'auth',
                'Authentication not configured',
                'Configure Google Photos Android AUTH_DATA in the Authentication page to begin uploading.',
                pref_key='notify_auth_issues',
            )

    def stop(self) -> None:
        self.stop_event.set()
        self._wake_supervisor.set()
        try:
            write_health(self.config.state_dir, False)
        except OSError:
            pass
        deadline = time.monotonic() + self.config.shutdown_timeout
        watcher_stop = Thread(target=self.watchers.stop_all, name='watcher-shutdown', daemon=True)
        watcher_stop.start()
        watcher_stop.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._supervisor_thread is not None and self._supervisor_thread.is_alive():
            self._supervisor_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if ((self._worker_thread is not None and self._worker_thread.is_alive())
                or watcher_stop.is_alive()):
            raise ShutdownTimeout('Shutdown deadline exceeded; pending work remains durable')
        self.store.record_activity('info', 'system', 'service_stopped', 'Sync Manager stopped cleanly')
        self.store.close()
        with self._lock:
            self._started = False

    def _supervisor_loop(self) -> None:
        while not self.stop_event.is_set():
            worker_alive = self._worker_thread is not None and self._worker_thread.is_alive()
            if not worker_alive:
                self._emit_notification(
                    'system:worker_stopped', 'error', 'system',
                    'Upload worker stopped unexpectedly',
                    'The background upload worker thread is not running. Pending queue entries remain durable in SQLite.',
                )
            else:
                self.store.resolve_notification('system:worker_stopped')

            folders = self.store.list_folders()
            health_by_id, needs_scan = self.watchers.check_all(folders)
            any_folder_unhealthy = False
            for folder in folders:
                fid = int(folder['id'])
                new_health = health_by_id.get(fid, 'unknown')
                issue_key = f'folder:{fid}:unavailable'
                if new_health == 'unavailable':
                    any_folder_unhealthy = True
                    self.store.update_folder_status(fid, health='unavailable', last_error='Folder unavailable or unreadable')
                    self._emit_notification(
                        issue_key, 'warning', 'folder',
                        f'Folder unavailable: {folder["name"]}',
                        f'Monitored path {folder["path"]} is currently unavailable. Watching will resume automatically when accessible.',
                        pref_key='notify_folder_unavailable',
                    )
                else:
                    self.store.update_folder_status(fid, health=new_health, clear_error=True)
                    self.store.resolve_notification(issue_key)

            watcher_info = self.watchers.watcher_summary()
            if not watcher_info['healthy']:
                self._emit_notification(
                    'monitor:watcher_stopped', 'warning', 'monitor',
                    'Folder watcher inactive',
                    'One or more folder watchers stopped and will be restarted automatically.',
                    pref_key='notify_watcher_stopped',
                )
            else:
                self.store.resolve_notification('monitor:watcher_stopped')

            now_mono = time.monotonic()
            scan_interval = float(self.get_settings()['scan_interval'])
            scan_all = now_mono >= self._next_scan_at
            for folder in folders:
                if self.stop_event.is_set():
                    break
                fid = int(folder['id'])
                if not folder['enabled'] or folder['paused']:
                    continue
                if not scan_all and fid not in needs_scan:
                    continue
                if health_by_id.get(fid) != 'healthy':
                    continue
                try:
                    self._run_folder_scan(folder)
                except OSError as error:
                    self.store.update_folder_status(fid, health='unavailable', last_error=type(error).__name__)
                    any_folder_unhealthy = True
            if scan_all:
                self._next_scan_at = time.monotonic() + scan_interval

            self._check_disk_space_notifications()
            try:
                write_health(self.config.state_dir, worker_alive and not any_folder_unhealthy)
            except OSError:
                pass

            self._wake_supervisor.wait(timeout=5.0)
            self._wake_supervisor.clear()

    # --- Internal Callbacks ---

    def _emit_notification(
        self,
        issue_key: str,
        severity: str,
        category: str,
        title: str,
        message: str,
        pref_key: str | None = None,
    ) -> None:
        if pref_key is not None and not self.get_settings().get(pref_key, True):
            return
        notification, created = self.store.upsert_notification(
            issue_key, severity, category, title, message
        )
        if created:
            self.events.publish('notification', notification)

    def _submit_from_folder(self, path: Path, folder_id: int) -> bool:
        folder = self.store.get_folder(folder_id)
        if folder is None:
            return False
        account_id = folder.get('account_id')
        if account_id is None:
            # Never route files from an unbound folder to another Google account.
            return False
        return self.processor.submit(path, folder_id=folder_id, account_id=int(account_id))

    def _on_file_detected(self, folder_id: int, path: Path) -> None:
        self.store.update_folder_status(folder_id, last_detected_at=time.time())
        self.events.publish('queue_updated', {'reason': 'file_detected', 'folder_id': folder_id})

    def _on_batch_start(
        self,
        ready: Sequence[tuple[dict[str, Any], Path, tuple[int, ...]]],
        threads: int,
    ) -> None:
        folder_names = {int(f['id']): str(f['name']) for f in self.store.list_folders()}
        account_names = {int(a['id']): str(a['name']) for a in self.auth.list_accounts()}
        self.events.start_batch(ready, threads, folder_names=folder_names, account_names=account_names)

    def _on_progress(self, event: Mapping[str, Any]) -> None:
        self.events.handle_progress(event)

    def _on_file_confirmed(
        self,
        job: dict[str, Any],
        media_key: str,
        result_status: str,
        duration: float,
    ) -> None:
        folder_id = job.get('folder_id')
        folder = self.store.get_folder(folder_id) if folder_id is not None else None
        folder_name = folder['name'] if folder else None
        account_id = job.get('account_id')
        account = self.auth.get_account(int(account_id)) if account_id is not None else None
        account_name = account['name'] if account else None
        self.store.record_history(
            job['path'],
            folder_id=folder_id,
            folder_name=folder_name,
            account_id=account_id,
            account_name=account_name,
            file_size=int(job.get('file_size') or 0),
            outcome=result_status,
            media_key=media_key,
            duration=round(duration, 2),
        )
        filename = Path(job['path']).name
        target_label = f' -> {account_name}' if account_name else ''
        self.store.record_activity(
            'info', 'upload',
            'upload_skipped' if result_status == 'skipped' else 'upload_completed',
            f'{"Duplicate skipped" if result_status == "skipped" else "Uploaded"} {filename}{target_label} ({duration:.2f}s)',
            details={
                'filename': filename,
                'outcome': result_status,
                'duration': round(duration, 2),
                'account_id': account_id,
            },
        )
        self.store.resolve_notification('upload:failures')
        if self.get_settings().get('notify_upload_completed', False):
            self._emit_notification(
                f'upload:done:{job["path"]}', 'success', 'upload',
                'Upload completed',
                f'{filename}{target_label} {"confirmed as existing remote duplicate" if result_status == "skipped" else "uploaded successfully"}.',
                pref_key='notify_upload_completed',
            )
        self.events.publish('queue_updated', {'reason': result_status})

    def _on_file_failed(
        self,
        job: dict[str, Any],
        error: Exception,
        attempts: int,
        delay: float,
        exhausted: bool,
    ) -> None:
        account_id = job.get('account_id')
        affected_aid = self.auth.report_runtime_auth_error(error, account_id=account_id)
        if affected_aid is not None:
            affected = self.auth.get_account(affected_aid)
            if affected and affected['status'] in ('invalid', 'expired'):
                self._emit_notification(
                    f'auth:account:{affected_aid}:invalid', 'error', 'auth',
                    f'Authentication issue: {affected["name"]}',
                    affected.get('last_error') or 'Update Android AUTH_DATA in the Accounts page.',
                    pref_key='notify_auth_issues',
                )

        folder_id = job.get('folder_id')
        folder = self.store.get_folder(folder_id) if folder_id is not None else None
        folder_name = folder['name'] if folder else None
        account = self.auth.get_account(int(account_id)) if account_id is not None else None
        account_name = account['name'] if account else None
        err_name = type(error).__name__
        filename = Path(job['path']).name
        self.store.record_history(
            job['path'],
            folder_id=folder_id,
            folder_name=folder_name,
            account_id=account_id,
            account_name=account_name,
            file_size=int(job.get('file_size') or 0),
            outcome='failed',
            error=err_name,
        )
        self.store.record_activity(
            'error' if exhausted else 'warning',
            'retry' if not exhausted else 'upload',
            'upload_failed' if exhausted else 'upload_retry_scheduled',
            f'Upload {"failed" if exhausted else "retry scheduled"} for {filename} ({err_name}, attempt {attempts}, retry in {delay:.1f}s)',
            details={
                'filename': filename,
                'error': err_name,
                'attempts': attempts,
                'exhausted': exhausted,
                'account_id': account_id,
            },
        )
        self._emit_notification(
            'upload:failures', 'warning' if not exhausted else 'error', 'upload',
            'Upload failures detected',
            f'{filename} failed ({err_name}, attempt {attempts}). Original local file preserved.',
            pref_key='notify_upload_failed',
        )
        self.events.publish('queue_updated', {'reason': 'failed'})

    def _on_batch_complete(self, summary: dict[str, Any]) -> None:
        self.events.finish_batch(summary)

    def _run_folder_scan(self, folder: Mapping[str, Any]) -> dict[str, Any]:
        result = self.watchers.scan_folder(folder, stop_event=self.stop_event)
        now = time.time()
        self._last_scan_at = now
        self.store.update_folder_status(
            int(folder['id']),
            health='healthy',
            last_scan_at=now,
            discovered_count=result['discovered_count'],
            last_detected_at=now if result['newly_queued'] > 0 else None,
            clear_error=True,
        )
        if result['newly_queued'] > 0:
            self.events.publish('queue_updated', {'reason': 'scan', 'folder_id': folder['id']})
        return result

    def _check_disk_space_notifications(self) -> None:
        threshold_bytes = int(float(self.get_settings()['low_disk_threshold_gb']) * (1024 ** 3))
        try:
            usage = shutil.disk_usage(self.config.state_dir)
            if usage.free < threshold_bytes:
                free_gb = usage.free / (1024 ** 3)
                self._emit_notification(
                    'disk:state_low', 'warning', 'system',
                    'State storage disk space low',
                    f'Only {free_gb:.2f} GB free on state storage volume.',
                    pref_key='notify_low_disk',
                )
            else:
                self.store.resolve_notification('disk:state_low')
        except OSError:
            pass

    # --- Public Service API Methods ---

    def is_queue_paused(self) -> bool:
        with self._lock:
            return self._queue_paused

    def pause_queue(self) -> dict[str, Any]:
        with self._lock:
            self._queue_paused = True
        self.store.record_activity('info', 'monitor', 'queue_paused', 'Upload queue paused (in-flight batches finish safely)')
        self.events.publish('queue_updated', {'queue_paused': True})
        return {'queue_paused': True}

    def resume_queue(self) -> dict[str, Any]:
        with self._lock:
            self._queue_paused = False
        self.store.record_activity('info', 'monitor', 'queue_resumed', 'Upload queue resumed')
        self.events.publish('queue_updated', {'queue_paused': False})
        return {'queue_paused': False}

    def browse_directories(self, path: str | None = None) -> dict[str, Any]:
        return browse_approved_directories(path, self.approved_roots)

    def add_folder(
        self,
        *,
        name: str,
        path: str,
        enabled: bool = True,
        recursive: bool = True,
        extensions: Sequence[str] | None = None,
        exclude_dirs: Sequence[str] | None = None,
        exclude_patterns: Sequence[str] | None = None,
        min_file_size: int = 1,
        max_file_size: int | None = None,
        account_id: int | None = None,
        create_subfolder_name: str | None = None,
        immich_preset: bool = False,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError('Folder name cannot be empty')
        resolved_account_id = account_id if account_id is not None else self.auth.get_primary_account_id()
        if resolved_account_id is not None and self.auth.get_account(int(resolved_account_id)) is None:
            raise ValueError('Selected Google account does not exist')
        if create_subfolder_name and create_subfolder_name.strip():
            resolved = create_dedicated_subfolder(path, create_subfolder_name, self.approved_roots)
        else:
            resolved = validate_approved_path(path, self.approved_roots, require_exists=not allow_missing)
        if self.store.get_folder_by_path(resolved) is not None:
            raise ValueError('This folder is already monitored')
        norm_exts = normalize_extensions(extensions)
        raw_excludes = list(exclude_dirs or ())
        raw_patterns = list(exclude_patterns or ())
        if immich_preset:
            raw_excludes.extend(IMMICH_EXCLUDE_DIRS)
            raw_patterns.extend(IMMICH_EXCLUDE_PATTERNS)
        norm_excludes = normalize_string_list(raw_excludes)
        norm_patterns = normalize_string_list(raw_patterns)
        if min_file_size < 1:
            raise ValueError('min_file_size must be at least 1 byte')
        if max_file_size is not None and max_file_size < min_file_size:
            raise ValueError('max_file_size must be at least min_file_size')
        folder = self.store.add_folder(
            name=cleaned_name,
            path=resolved,
            enabled=enabled,
            recursive=recursive,
            extensions=norm_exts,
            exclude_dirs=norm_excludes,
            exclude_patterns=norm_patterns,
            min_file_size=min_file_size,
            max_file_size=max_file_size,
            account_id=resolved_account_id,
        )
        self.store.record_activity(
            'info', 'folder', 'folder_added',
            f'Added monitored folder "{cleaned_name}" ({resolved})'
            + (f' -> {folder["account_name"]}' if folder.get('account_name') else ''),
            details={'folder_id': folder['id'], 'recursive': recursive, 'account_id': resolved_account_id},
        )
        self._wake_supervisor.set()
        if enabled and resolved_account_id is not None and resolved.is_dir():
            try:
                self._run_folder_scan(folder)
            except OSError:
                pass
        updated = self.store.get_folder(folder['id']) or folder
        self.events.publish('folders_updated', {'folder_id': folder['id']})
        return updated

    def update_folder(self, folder_id: int, updates: Mapping[str, Any]) -> dict[str, Any]:
        existing = self.store.get_folder(folder_id)
        if existing is None:
            raise KeyError('Folder not found')
        cleaned: dict[str, Any] = {}
        if 'name' in updates and updates['name'] is not None:
            name_val = str(updates['name']).strip()
            if not name_val:
                raise ValueError('Folder name cannot be empty')
            cleaned['name'] = name_val
        if 'enabled' in updates and updates['enabled'] is not None:
            cleaned['enabled'] = bool(updates['enabled'])
        if 'paused' in updates and updates['paused'] is not None:
            cleaned['paused'] = bool(updates['paused'])
        if 'recursive' in updates and updates['recursive'] is not None:
            cleaned['recursive'] = bool(updates['recursive'])
        if 'extensions' in updates and updates['extensions'] is not None:
            cleaned['extensions'] = normalize_extensions(updates['extensions'])
        if 'exclude_dirs' in updates and updates['exclude_dirs'] is not None:
            cleaned['exclude_dirs'] = normalize_string_list(updates['exclude_dirs'])
        if 'exclude_patterns' in updates and updates['exclude_patterns'] is not None:
            cleaned['exclude_patterns'] = normalize_string_list(updates['exclude_patterns'])
        if 'account_id' in updates:
            new_aid = updates['account_id']
            if new_aid is not None:
                if self.auth.get_account(int(new_aid)) is None:
                    raise ValueError('Selected Google account does not exist')
                cleaned['account_id'] = int(new_aid)
            else:
                cleaned['account_id'] = None
        min_size = int(updates['min_file_size']) if updates.get('min_file_size') is not None else int(existing['min_file_size'])
        max_size = updates['max_file_size'] if 'max_file_size' in updates else existing['max_file_size']
        if min_size < 1:
            raise ValueError('min_file_size must be at least 1 byte')
        if max_size is not None:
            max_size = int(max_size)
            if max_size < min_size:
                raise ValueError('max_file_size must be at least min_file_size')
        if 'min_file_size' in updates:
            cleaned['min_file_size'] = min_size
        if 'max_file_size' in updates:
            cleaned['max_file_size'] = max_size

        updated = self.store.update_folder(folder_id, **cleaned)
        assert updated is not None
        if not updated['enabled']:
            self.store.update_folder_status(folder_id, health='disabled')
        elif updated['paused']:
            self.store.update_folder_status(folder_id, health='paused')
        self.store.record_activity(
            'info', 'folder', 'folder_updated',
            f'Updated folder "{updated["name"]}"',
            details={'folder_id': folder_id, 'fields': list(cleaned.keys())},
        )
        self._wake_supervisor.set()
        if 'account_id' in cleaned and cleaned['account_id'] is not None and updated['enabled'] and not updated['paused']:
            try:
                self._run_folder_scan(updated)
            except OSError:
                pass
        refreshed = self.store.get_folder(folder_id) or updated
        self.events.publish('folders_updated', {'folder_id': folder_id})
        return refreshed

    def remove_folder(self, folder_id: int) -> bool:
        existing = self.store.get_folder(folder_id)
        if existing is None:
            raise KeyError('Folder not found')
        removed = self.store.remove_folder(folder_id)
        self.store.resolve_notification(f'folder:{folder_id}:unavailable')
        self.store.record_activity(
            'info', 'folder', 'folder_removed',
            f'Removed folder "{existing["name"]}" from monitoring (local files and queue records preserved)',
            details={'folder_id': folder_id},
        )
        self._wake_supervisor.set()
        self.events.publish('folders_updated', {'folder_id': folder_id})
        return removed

    def scan_folder_now(self, folder_id: int) -> dict[str, Any]:
        folder = self.store.get_folder(folder_id)
        if folder is None:
            raise KeyError('Folder not found')
        validate_approved_path(folder['path'], self.approved_roots, require_exists=True)
        result = self._run_folder_scan(folder)
        self.store.record_activity(
            'info', 'monitor', 'folder_scanned',
            f'Scanned "{folder["name"]}": {result["discovered_count"]} media files found, {result["newly_queued"]} newly queued',
            details=result,
        )
        self.events.publish('folders_updated', {'folder_id': folder_id})
        return {**result, 'folder': self.store.get_folder(folder_id)}

    def retry_job(self, path: str) -> bool:
        ok = self.store.retry_job(path)
        if ok:
            self.store.record_activity('info', 'retry', 'job_retried', f'Manual retry requested for {Path(path).name}')
            self.events.publish('queue_updated', {'reason': 'manual_retry'})
        return ok

    def retry_all_failed(self, folder_id: int | None = None) -> int:
        count = self.store.retry_all_failed(folder_id=folder_id)
        if count > 0:
            self.store.record_activity('info', 'retry', 'retry_all', f'Requeued {count} failed/retrying jobs')
            self.events.publish('queue_updated', {'reason': 'retry_all', 'count': count})
        return count

    def cancel_job(self, path: str) -> bool:
        ok = self.store.cancel_job(path)
        if ok:
            self.store.record_activity('info', 'upload', 'job_cancelled', f'Cancelled pending job for {Path(path).name} (local file untouched)')
            self.events.publish('queue_updated', {'reason': 'cancelled'})
        return ok

    # --- Immich Integration ---

    def get_immich_status(self) -> dict[str, Any]:
        return {
            **self.immich.get_status(),
            'approved_roots': [str(r) for r in self.approved_roots],
        }

    def configure_immich(
        self,
        *,
        url: str | None = None,
        public_url: str | None = None,
        media_root: str | None = None,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> dict[str, Any]:
        status = self.immich.configure(
            url=url,
            public_url=public_url,
            media_root=media_root,
            api_key=api_key,
            clear_api_key=clear_api_key,
        )
        self.store.record_activity(
            'info', 'settings', 'immich_configured',
            f'Updated Immich integration settings (url={status["url"] or "unset"}, api_key={"set" if status["has_api_key"] else "unset"})',
        )
        return {**status, 'approved_roots': [str(r) for r in self.approved_roots]}

    def list_immich_users(self) -> dict[str, Any]:
        data = self.immich.list_users()
        folders = self.store.list_folders()
        folders_by_path = {str(Path(f['path']).resolve()): f for f in folders}

        enriched_users = []
        for user in data['users']:
            bound = None
            for cand in user.get('candidate_paths', []):
                match = folders_by_path.get(str(Path(cand['path']).resolve()))
                if match is not None:
                    bound = match
                    break
            enriched_users.append({**user, 'bound_folder': bound})

        enriched_disk = []
        for entry in data['disk_folders']:
            match = folders_by_path.get(str(Path(entry['path']).resolve()))
            enriched_disk.append({**entry, 'bound_folder': match})

        return {
            **data,
            'users': enriched_users,
            'disk_folders': enriched_disk,
            'accounts': self.list_accounts(),
        }

    def bind_immich_folder(
        self,
        *,
        path: str,
        account_id: int,
        folder_name: str,
    ) -> dict[str, Any]:
        if self.auth.get_account(int(account_id)) is None:
            raise ValueError('Selected Google account does not exist')
        resolved = validate_approved_path(path, self.approved_roots, require_exists=False)
        existing = self.store.get_folder_by_path(resolved)
        if existing is not None:
            folder = self.update_folder(
                int(existing['id']),
                {'account_id': int(account_id), 'name': folder_name.strip() or existing['name'], 'enabled': True, 'paused': False},
            )
            return {'folder': folder, 'updated_existing': True}
        folder = self.add_folder(
            name=folder_name.strip() or f'Immich ({resolved.name})',
            path=str(resolved),
            account_id=int(account_id),
            enabled=True,
            recursive=True,
            immich_preset=True,
            allow_missing=True,
        )
        return {'folder': folder, 'updated_existing': False}

    def create_immich_user(
        self,
        *,
        name: str,
        email: str,
        password: str,
        storage_label: str | None = None,
        is_admin: bool = False,
        account_id: int | None = None,
        auto_bind: bool = False,
    ) -> dict[str, Any]:
        user = self.immich.create_user(
            name=name,
            email=email,
            password=password,
            storage_label=storage_label,
            is_admin=is_admin,
        )
        self.store.record_activity(
            'info', 'folder', 'immich_user_created',
            f'Created Immich user "{user["name"]}"',
            details={'storage_label': user.get('storage_label')},
        )
        bound_folder = None
        bind_warning = None
        if auto_bind and account_id is not None and user.get('recommended_path'):
            try:
                res = self.bind_immich_folder(
                    path=user['recommended_path'],
                    account_id=int(account_id),
                    folder_name=f'Immich - {user["name"]}',
                )
                bound_folder = res['folder']
            except (PermissionError, ValueError) as exc:
                bind_warning = str(exc)
        return {'user': user, 'folder': bound_folder, 'bind_warning': bind_warning}

    # --- Multi-Account Management ---

    def list_accounts(self) -> list[dict[str, Any]]:
        return self.auth.list_accounts()

    def add_account(
        self,
        *,
        name: str,
        auth_data: str,
        create_dedicated_folder: bool = False,
        parent_root: str | None = None,
        bind_folder_path: str | None = None,
        folder_name: str | None = None,
        immich_preset: bool = False,
    ) -> dict[str, Any]:
        result = self.auth.add_account(name, auth_data)
        if not result.get('saved'):
            self.store.record_activity(
                'warning', 'auth', 'account_add_rejected',
                f'Rejected connecting Google account "{name}" ({result.get("status", "invalid")})',
            )
            return result

        account = result['account']
        created_folder: dict[str, Any] | None = None
        folder_warning: str | None = None
        if create_dedicated_folder:
            target_root = parent_root.strip() if parent_root and parent_root.strip() else str(self.config.root)
            try:
                created_folder = self.add_folder(
                    name=(folder_name or f'{account["name"]} Folder').strip(),
                    path=target_root,
                    create_subfolder_name=account['slug'],
                    account_id=int(account['id']),
                    recursive=bool(self.get_settings().get('default_recursive', True)),
                    immich_preset=immich_preset,
                )
            except (PermissionError, ValueError) as error:
                folder_warning = str(error)
        elif bind_folder_path and bind_folder_path.strip():
            try:
                created_folder = self.add_folder(
                    name=(folder_name or f'{account["name"]} Folder').strip(),
                    path=bind_folder_path.strip(),
                    account_id=int(account['id']),
                    recursive=bool(self.get_settings().get('default_recursive', True)),
                    immich_preset=immich_preset,
                )
            except (PermissionError, ValueError) as error:
                folder_warning = str(error)

        self.store.resolve_notification('auth:not_configured')
        self.store.record_activity(
            'info', 'auth', 'account_added',
            f'Connected Google account "{account["name"]}" ({account["account_hint"]})',
            details={'account_id': account['id'], 'folder_id': created_folder['id'] if created_folder else None},
        )
        self.events.publish('auth_updated', {'accounts': self.auth.list_accounts(), 'auth': self.auth.status()})
        refreshed_account = self.auth.get_account(int(account['id'])) or account
        return {
            **result,
            'account': refreshed_account,
            'folder': created_folder,
            'folder_warning': folder_warning,
        }

    def rename_account(self, account_id: int, name: str) -> dict[str, Any]:
        updated = self.auth.rename_account(account_id, name)
        self.store.record_activity(
            'info', 'auth', 'account_renamed',
            f'Renamed Google account #{account_id} to "{updated["name"]}"',
        )
        self.events.publish('auth_updated', {'accounts': self.auth.list_accounts(), 'auth': self.auth.status()})
        return updated

    def rotate_account_credentials(self, account_id: int, auth_data: str) -> dict[str, Any]:
        result = self.auth.replace_account_credentials(account_id, auth_data)
        if result.get('saved'):
            self.store.resolve_notification(f'auth:account:{account_id}:invalid')
            self.store.resolve_notification('auth:invalid')
            self.store.resolve_notification('auth:not_configured')
            acc = result['account']
            self.store.record_activity(
                'info', 'auth', 'auth_updated',
                f'Rotated Android credentials for "{acc["name"]}" ({acc["account_hint"]})',
                details={'account_id': account_id},
            )
            self.events.publish('auth_updated', {'accounts': self.auth.list_accounts(), 'auth': self.auth.status()})
        else:
            self.store.record_activity(
                'warning', 'auth', 'auth_update_rejected',
                f'Rejected invalid credential rotation for account #{account_id}; existing credentials preserved',
                details={'account_id': account_id},
            )
        return result

    def revalidate_account(self, account_id: int) -> dict[str, Any]:
        result = self.auth.revalidate_account(account_id)
        acc = result['account']
        if result['valid']:
            self.store.resolve_notification(f'auth:account:{account_id}:invalid')
            self.store.resolve_notification('auth:invalid')
        else:
            self._emit_notification(
                f'auth:account:{account_id}:invalid', 'error', 'auth',
                f'Authentication issue: {acc["name"]}',
                result.get('message') or 'Revalidation failed.',
                pref_key='notify_auth_issues',
            )
        self.store.record_activity(
            'info' if result['valid'] else 'warning',
            'auth', 'auth_revalidated',
            f'Revalidated "{acc["name"]}": {result["status"]}',
            details={'account_id': account_id},
        )
        self.events.publish('auth_updated', {'accounts': self.auth.list_accounts(), 'auth': self.auth.status()})
        return result

    def remove_account(self, account_id: int, *, force: bool = False) -> dict[str, Any]:
        account = self.auth.get_account(account_id)
        if account is None:
            raise KeyError('Account not found')
        bound_folders = [f for f in self.store.list_folders() if f.get('account_id') == account_id]
        if bound_folders and not force:
            names = ', '.join(f['name'] for f in bound_folders)
            raise ValueError(
                f'Account "{account["name"]}" is still bound to folder(s): {names}. Reassign them or confirm forced disconnect to pause & unbind them.'
            )
        removed = self.auth.remove_account(account_id)
        self.store.resolve_notification(f'auth:account:{account_id}:invalid')
        self.store.record_activity(
            'info', 'auth', 'account_removed',
            f'Disconnected Google account "{account["name"]}" (local media and history preserved)',
            details={'account_id': account_id, 'unbound_folders': len(bound_folders)},
        )
        self._wake_supervisor.set()
        self.events.publish('auth_updated', {'accounts': self.auth.list_accounts(), 'auth': self.auth.status()})
        self.events.publish('folders_updated', {'reason': 'account_removed'})
        return {**removed, 'unbound_folders': len(bound_folders)}

    def validate_auth(self, auth_data: str) -> dict[str, Any]:
        result = self.auth.validate_candidate(auth_data)
        self.store.record_activity(
            'info' if result['valid'] else 'warning',
            'auth', 'auth_validated',
            f'Android credential validation {"succeeded" if result["valid"] else "failed"} ({result["status"]})',
        )
        return result

    def save_auth(self, auth_data: str) -> dict[str, Any]:
        result = self.auth.save_validated_credentials(auth_data)
        if result.get('saved'):
            self.store.resolve_notification('auth:invalid')
            self.store.resolve_notification('auth:not_configured')
            if result.get('account'):
                self.store.resolve_notification(f'auth:account:{result["account"]["id"]}:invalid')
            self.store.record_activity(
                'info', 'auth', 'auth_updated',
                f'Android credentials updated safely ({result.get("account_hint", "masked")})',
            )
            self.events.publish('auth_updated', self.auth.status())
        else:
            self.store.record_activity(
                'warning', 'auth', 'auth_update_rejected',
                f'Rejected invalid credential update ({result["status"]}); existing credentials preserved',
            )
        return result

    def revalidate_auth(self) -> dict[str, Any]:
        result = self.auth.revalidate_current()
        if result['valid']:
            self.store.resolve_notification('auth:invalid')
            self.store.resolve_notification('auth:not_configured')
        else:
            self._emit_notification(
                'auth:invalid', 'error', 'auth',
                'Google Photos authentication issue',
                result.get('message') or 'Revalidation failed.',
                pref_key='notify_auth_issues',
            )
        self.store.record_activity(
            'info' if result['valid'] else 'warning',
            'auth', 'auth_revalidated',
            f'Current credentials revalidation result: {result["status"]}',
        )
        self.events.publish('auth_updated', self.auth.status())
        return result

    def get_overview(self) -> dict[str, Any]:
        stats = self.store.queue_stats()
        folders = self.store.list_folders()
        accounts = self.auth.list_accounts()
        live = self.events.live_snapshot()
        recent_history, _ = self.store.list_history(limit=8)
        recent_activity, _ = self.store.list_activity(limit=8)
        notifications, unread = self.store.list_notifications(limit=10)
        settings = self.get_settings()
        return {
            'stats': stats,
            'queue_paused': self.is_queue_paused(),
            'live': live,
            'upload_threads': settings['upload_threads'],
            'upload_batch_size': settings['upload_batch_size'],
            'folders': folders,
            'accounts': accounts,
            'auth': self.auth.status(),
            'watcher': self.watchers.watcher_summary(),
            'recent_history': recent_history,
            'recent_activity': recent_activity,
            'notifications': notifications,
            'unread_notifications': unread,
        }

    def get_system_health(self) -> dict[str, Any]:
        worker_alive = self._worker_thread is not None and self._worker_thread.is_alive()
        supervisor_alive = self._supervisor_thread is not None and self._supervisor_thread.is_alive()
        auth_status = self.auth.status()
        watcher_summary = self.watchers.watcher_summary()
        folders = self.store.list_folders()
        db_info = self.store.db_stats()
        stats = self.store.queue_stats()
        live = self.events.live_snapshot()
        settings = self.get_settings()
        threshold_bytes = int(float(settings['low_disk_threshold_gb']) * (1024 ** 3))

        disks: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for label, target_path in [('State Storage', self.config.state_dir), *[(f'Media Root: {r.name or r}', r) for r in self.approved_roots]]:
            key = str(target_path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            try:
                usage = shutil.disk_usage(target_path)
                disks.append({
                    'label': label,
                    'path': key,
                    'available': True,
                    'total_bytes': usage.total,
                    'used_bytes': usage.used,
                    'free_bytes': usage.free,
                    'free_percent': round((usage.free * 100.0) / usage.total, 1) if usage.total > 0 else 0.0,
                    'low_disk': usage.free < threshold_bytes,
                })
            except OSError:
                disks.append({
                    'label': label,
                    'path': key,
                    'available': False,
                    'total_bytes': 0,
                    'used_bytes': 0,
                    'free_bytes': 0,
                    'free_percent': 0.0,
                    'low_disk': False,
                })

        max_threads = int(settings['upload_threads'])
        active_uploads = int(live['active_count'])
        accounts = self.auth.list_accounts()
        any_valid_account = any(acc['status'] == 'valid' for acc in accounts) if accounts else auth_status['status'] == 'valid'
        all_accounts_healthy = all(acc['status'] == 'valid' for acc in accounts) if accounts else auth_status['status'] == 'valid'
        overall_ok = (
            db_info['ok']
            and (not self._started or (worker_alive and supervisor_alive))
            and any_valid_account
            and all_accounts_healthy
            and watcher_summary['healthy']
        )

        last_scan_times = [f['last_scan_at'] for f in folders if f.get('last_scan_at')]
        if self._last_scan_at:
            last_scan_times.append(self._last_scan_at)

        return {
            'status': 'healthy' if overall_ok else 'degraded',
            'app_version': APP_VERSION,
            'uptime_seconds': round(time.time() - self.started_at, 1),
            'backend': {'status': 'healthy', 'running': True},
            'uploader': {
                'status': 'running' if (worker_alive or not self._started) else 'stopped',
                'worker_alive': worker_alive,
                'queue_paused': self.is_queue_paused(),
            },
            'auth': auth_status,
            'accounts': accounts,
            'watcher': {
                **watcher_summary,
                'supervisor_alive': supervisor_alive,
                'folders': [
                    {
                        'id': f['id'],
                        'name': f['name'],
                        'path': f['path'],
                        'account_id': f.get('account_id'),
                        'account_name': f.get('account_name'),
                        'health': f['health'],
                        'enabled': f['enabled'],
                        'paused': f['paused'],
                        'last_scan_at': f['last_scan_at'],
                        'last_error': f['last_error'],
                    }
                    for f in folders
                ],
            },
            'database': db_info,
            'disks': disks,
            'upload_slots': {
                'active': active_uploads,
                'max_threads': max_threads,
                'available': max(0, max_threads - active_uploads),
                'batch_size': int(settings['upload_batch_size']),
            },
            'last_successful_upload_at': stats.get('last_completed_at'),
            'last_successful_scan_at': max(last_scan_times) if last_scan_times else None,
        }
