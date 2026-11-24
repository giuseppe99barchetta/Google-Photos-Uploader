import json
import logging
import os
import signal
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
from urllib.parse import parse_qs, quote_plus

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver as Observer

from upload_state import StateStore
from uploader_config import Config

MEDIA_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.heic', '.mp4', '.mov', '.mkv')
# macOS ._ files and dot-folders, Synology @eaDir thumbnails and #recycle bins.
SKIPPED_PREFIXES = ('.', '@', '#')
ACTIVE_UPLOAD_PHASES = frozenset({'hashing', 'checking', 'uploading', 'finalizing'})
TERMINAL_UPLOAD_PHASES = frozenset({'complete', 'skipped', 'error'})
FileSignature = tuple[int, int, int, int]
BatchCandidate = tuple[dict[str, Any], Path, FileSignature]
logger = logging.getLogger('gphotos_uploader')


class RedactingFormatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self, auth_data: str = '') -> None:
        super().__init__('%(asctime)sZ %(levelname)s %(threadName)s %(message)s',
                         datefmt='%Y-%m-%dT%H:%M:%S')
        self._lock = Lock()
        self.secrets: list[str] = []
        self.add_secrets(auth_data)

    def add_secrets(self, auth_data: str) -> None:
        if not auth_data:
            return
        parsed = parse_qs(auth_data)
        tokens = parsed.get('Token', []) + parsed.get('Auth', [])
        additions = {
            value
            for token in [auth_data, *tokens]
            for value in (token, quote_plus(token))
            if value
        }
        with self._lock:
            merged = set(self.secrets) | additions
            self.secrets = sorted(merged, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        with self._lock:
            secrets_snapshot = list(self.secrets)
        for secret in secrets_snapshot:
            message = message.replace(secret, '[REDACTED]')
        return message


_active_formatter: RedactingFormatter | None = None


def configure_logging(config: Config) -> RedactingFormatter:
    global _active_formatter
    handler = logging.StreamHandler()
    formatter = RedactingFormatter(config.auth_data)
    if config.dashboard_password:
        formatter.add_secrets(config.dashboard_password)
    if config.dashboard_session_secret:
        formatter.add_secrets(config.dashboard_session_secret)
    handler.setFormatter(formatter)
    logging.basicConfig(level=config.log_level, handlers=[handler], force=True)
    _active_formatter = formatter
    return formatter


def register_redacted_secret(secret_text: str) -> None:
    if _active_formatter is not None and secret_text:
        _active_formatter.add_secrets(secret_text)


def install_signal_handlers(stop_event):
    def request_stop(signum, frame):
        stop_event.set()

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, request_stop)
    return previous


class ShutdownTimeout(RuntimeError):
    pass


class FileChangedError(RuntimeError):
    pass


def file_signature(path: str | Path) -> FileSignature:
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'Not a regular file: {path}')
    # No ctime: chmod/chown must not make the whole library look new.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def is_media(path: str | Path, root: str | Path) -> bool:
    """root: the watched folder as written in the paths being checked (not necessarily resolved)."""
    path = Path(path)
    root = Path(root)
    if path.suffix.lower() not in MEDIA_EXTENSIONS or path.is_symlink():
        return False
    # Check the resolved path too, so a symlink inside a hidden folder cannot bypass the rule.
    # Each form is compared with the same form of root (Windows short names differ from resolved ones).
    for candidate, base in ((path, root), (path.resolve(), root.resolve())):
        if candidate.is_relative_to(base) and any(
                part.startswith(SKIPPED_PREFIXES) for part in candidate.relative_to(base).parts):
            return False
    return True


def _confirmed_media_key(result: Any, path: Path) -> str | None:
    if not isinstance(result, Mapping):
        return None
    media_key = result.get(path.as_posix())
    if isinstance(media_key, str) and media_key.strip():
        return media_key
    return None


def wait_until_stable(
    path: str | Path,
    stable_seconds: float = 5,
    timeout: float = 300,
    stop_event: Event | None = None,
    known: tuple[FileSignature, float] | None = None,
) -> FileSignature:
    stop_event = stop_event or Event()
    deadline = time.monotonic() + timeout
    # known=(signature, monotonic time first seen) lets an already-quiet file skip the wait.
    previous, unchanged_since = known or (None, time.monotonic())
    while not stop_event.is_set():
        signature = file_signature(path)
        now = time.monotonic()
        if signature != previous:
            previous, unchanged_since = signature, now
        if signature[2] > 0 and now - unchanged_since >= stable_seconds:
            return signature
        if now >= deadline:
            raise TimeoutError(f'File is empty or still changing: {path}')
        stop_event.wait(min(1, max(0.01, stable_seconds / 2), deadline - now))
    raise InterruptedError('Upload cancelled during shutdown')


def wait_until_stable_batch(
    candidates: Sequence[BatchCandidate],
    stable_seconds: float = 5,
    stability_timeout: float = 300,
    stop_event: Event | None = None,
    known_map: Mapping[str, tuple[FileSignature, float]] | None = None,
) -> tuple[list[BatchCandidate], list[BatchCandidate], list[tuple[dict[str, Any], Exception]]]:
    stop_event = stop_event or Event()
    if stop_event.is_set():
        raise InterruptedError('Upload cancelled during shutdown')
    now = time.monotonic()
    deadline = now + stability_timeout
    previous: dict[Path, FileSignature] = {}
    unchanged_since: dict[Path, float] = {}
    for _, path, signature in candidates:
        seen = known_map.get(str(path)) if known_map else None
        if seen is not None and seen[0] == signature:
            previous[path] = seen[0]
            unchanged_since[path] = seen[1]
        else:
            previous[path] = signature
            unchanged_since[path] = now
    pending = list(candidates)
    ready: list[BatchCandidate] = []
    changed_deferred: list[BatchCandidate] = []
    failed: list[tuple[dict[str, Any], Exception]] = []

    while pending and not stop_event.is_set():
        still_waiting: list[BatchCandidate] = []
        for job, path, initial_sig in pending:
            try:
                signature = file_signature(path)
            except Exception as error:
                failed.append((job, error))
                continue
            now = time.monotonic()
            if signature != previous[path]:
                previous[path] = signature
                unchanged_since[path] = now
            if signature[2] > 0 and now - unchanged_since[path] >= stable_seconds:
                ready.append((job, path, signature))
            else:
                still_waiting.append((job, path, initial_sig))
        pending = still_waiting
        if not pending:
            return ready, changed_deferred, failed
        if ready:
            # Do not stall ready files behind another file that is still growing or empty.
            for job, path, initial_sig in pending:
                if previous[path] != initial_sig:
                    changed_deferred.append((job, path, previous[path]))
            return ready, changed_deferred, failed
        now = time.monotonic()
        if now >= deadline:
            for job, path, _ in pending:
                failed.append((job, TimeoutError(f'File is empty or still changing: {path}')))
            return ready, changed_deferred, failed
        stop_event.wait(min(1, max(0.01, stable_seconds / 2), deadline - now))
    raise InterruptedError('Upload cancelled during shutdown')


class BatchProgressTracker:
    """Thread-safe tracker for concurrent GPMC upload progress events."""

    def __init__(self, batch_size: int, progress_interval: float = 15.0) -> None:
        self.batch_size = batch_size
        self.progress_interval = progress_interval
        self._lock = Lock()
        self._active_paths: set[str] = set()
        self._phases: dict[str, str] = {}
        self._terminal_phases: dict[str, str] = {}
        self._last_bucket: dict[str, int] = {}
        self._last_logged_at: dict[str, float] = {}
        self._max_active = 0

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active_paths)

    @property
    def max_active(self) -> int:
        with self._lock:
            return self._max_active

    def terminal_phase(self, posix_path: str) -> str | None:
        with self._lock:
            return self._terminal_phases.get(posix_path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                'batch_size': self.batch_size,
                'active': len(self._active_paths),
                'max_active': self._max_active,
                'phases': dict(self._phases),
                'terminal_phases': dict(self._terminal_phases),
            }

    def handle_event(self, event: Mapping[str, Any]) -> None:
        if not isinstance(event, Mapping):
            return
        phase = str(event.get('phase') or '')
        if not phase:
            return
        path = str(event.get('path') or event.get('filename') or '<unknown>')
        try:
            bytes_completed = int(event.get('bytes_completed') or 0)
        except (TypeError, ValueError):
            bytes_completed = 0
        try:
            bytes_total = int(event.get('bytes_total') or 0)
        except (TypeError, ValueError):
            bytes_total = 0
        percent = min(100, max(0, int(bytes_completed * 100 / bytes_total))) if bytes_total > 0 else 0
        now = time.monotonic()

        log_level: int | None = None
        with self._lock:
            previous_phase = self._phases.get(path)
            self._phases[path] = phase
            if phase in ACTIVE_UPLOAD_PHASES:
                self._active_paths.add(path)
                if len(self._active_paths) > self._max_active:
                    self._max_active = len(self._active_paths)
            elif phase in TERMINAL_UPLOAD_PHASES:
                self._active_paths.discard(path)
                self._terminal_phases[path] = phase
            active = len(self._active_paths)

            if phase == 'uploading':
                bucket = percent // 25
                last_bucket = self._last_bucket.get(path, -1)
                last_logged = self._last_logged_at.get(path, 0.0)
                if (previous_phase != 'uploading' or bucket > last_bucket
                        or (now - last_logged >= self.progress_interval and bytes_completed > 0)):
                    self._last_bucket[path] = max(bucket, last_bucket)
                    self._last_logged_at[path] = now
                    log_level = logging.INFO
            elif phase == 'skipped':
                if previous_phase != 'skipped':
                    log_level = logging.INFO
            elif phase == 'error':
                if previous_phase != 'error':
                    log_level = logging.WARNING
            elif phase != previous_phase:
                log_level = logging.DEBUG

        if log_level is not None:
            if phase == 'uploading':
                logger.log(
                    log_level,
                    'Upload progress file=%s phase=%s percent=%d%% bytes=%d/%d active=%d/%d',
                    path, phase, percent, bytes_completed, bytes_total, active, self.batch_size,
                )
            else:
                logger.log(
                    log_level,
                    'Upload progress file=%s phase=%s active=%d/%d',
                    path, phase, active, self.batch_size,
                )


def upload_file(
    client: Any,
    path: str | Path,
    stable_seconds: float = 5,
    stability_timeout: float = 300,
    stop_event: Event | None = None,
    on_confirmed: Callable[[FileSignature, str], None] | None = None,
    known: tuple[FileSignature, float] | None = None,
) -> str:
    path = Path(path).absolute()
    signature = wait_until_stable(path, stable_seconds, stability_timeout, stop_event, known)
    started = time.monotonic()
    result = client.upload(target=str(path), show_progress=False, delete_from_host=False)
    media_key = _confirmed_media_key(result, path)
    if not media_key:
        raise RuntimeError(f'Upload not confirmed: {path}')
    if file_signature(path) != signature:
        raise FileChangedError(f'File changed during upload; original retained: {path}')
    if on_confirmed:
        on_confirmed(signature, media_key)
    logger.info('Upload confirmed file=%s duration=%.2fs', path, time.monotonic() - started)
    return media_key


class FileProcessor:
    def __init__(
        self,
        client: Any,
        store: StateStore,
        delete_after_upload: bool = False,
        stop_event: Event | None = None,
        max_attempts: int = 5,
        retry_base: float = 5,
        retry_max: float = 3600,
        root: str | Path | None = None,
        allowed_roots: Sequence[str | Path] | None = None,
        stable_seconds: float = 5,
        stability_timeout: float = 300,
        upload_threads: int = 2,
        upload_batch_size: int = 4,
        upload_quality: str = 'original',
        use_quota: bool = False,
        duplicate_detection: bool = True,
        filename_conflict_policy: str = 'hash_dedup',
        auto_retry_failed: bool = True,
        settings_provider: Callable[[], Mapping[str, Any]] | None = None,
        client_provider: Callable[[], Any] | None = None,
        account_client_provider: Callable[[int | None], Any] | None = None,
        eligible_accounts_provider: Callable[[], Sequence[int]] | None = None,
        default_account_provider: Callable[[], int | None] | None = None,
        is_paused: Callable[[], bool] | None = None,
        on_progress: Callable[[Mapping[str, Any]], None] | None = None,
        on_batch_start: Callable[[Sequence[BatchCandidate], int], None] | None = None,
        on_batch_complete: Callable[[dict[str, Any]], None] | None = None,
        on_file_confirmed: Callable[[dict[str, Any], str, str, float], None] | None = None,
        on_file_failed: Callable[[dict[str, Any], Exception, int, float, bool], None] | None = None,
    ) -> None:
        if upload_threads < 1:
            raise ValueError('upload_threads must be at least 1')
        if upload_batch_size < 1:
            raise ValueError('upload_batch_size must be at least 1')
        self.client = client
        self.store = store
        self.delete_after_upload = delete_after_upload
        self.stop_event = stop_event or Event()
        self.max_attempts = max_attempts
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.stable_seconds = stable_seconds
        self.stability_timeout = stability_timeout
        self.upload_threads = upload_threads
        self.upload_batch_size = upload_batch_size
        self.upload_quality = upload_quality
        self.use_quota = use_quota
        self.duplicate_detection = duplicate_detection
        self.filename_conflict_policy = filename_conflict_policy
        self.auto_retry_failed = auto_retry_failed
        self.settings_provider = settings_provider
        self.client_provider = client_provider
        self.account_client_provider = account_client_provider
        self.eligible_accounts_provider = eligible_accounts_provider
        self.default_account_provider = default_account_provider
        self.is_paused = is_paused
        self.on_progress = on_progress
        self.on_batch_start = on_batch_start
        self.on_batch_complete = on_batch_complete
        self.on_file_confirmed = on_file_confirmed
        self.on_file_failed = on_file_failed
        self._last_account_id: int | None = None
        self.root = Path(root).resolve() if root is not None else None
        self.allowed_roots = (
            tuple(Path(item).resolve() for item in allowed_roots)
            if allowed_roots is not None
            else None
        )
        self.first_seen: dict[str, tuple[FileSignature, float]] = {}  # path -> (sig, monotonic time)
        self.seen_lock = Lock()

    def _effective_settings(self) -> dict[str, Any]:
        base = {
            'delete_after_upload': self.delete_after_upload,
            'max_attempts': self.max_attempts,
            'retry_base': self.retry_base,
            'retry_max': self.retry_max,
            'stable_seconds': self.stable_seconds,
            'stability_timeout': self.stability_timeout,
            'upload_threads': self.upload_threads,
            'upload_batch_size': self.upload_batch_size,
            'upload_quality': self.upload_quality,
            'use_quota': self.use_quota,
            'duplicate_detection': self.duplicate_detection,
            'filename_conflict_policy': self.filename_conflict_policy,
            'auto_retry_failed': self.auto_retry_failed,
        }
        if self.settings_provider is not None:
            overrides = self.settings_provider()
            for key in base:
                if key in overrides:
                    base[key] = overrides[key]
        return base

    def _current_client(self, account_id: int | None = None) -> Any:
        if self.account_client_provider is not None:
            return self.account_client_provider(account_id)
        if self.client_provider is not None:
            return self.client_provider()
        return self.client

    def check_path(self, path: Path) -> None:
        resolved = path.resolve()
        if self.allowed_roots is not None:
            if not any(resolved.is_relative_to(root) for root in self.allowed_roots):
                raise ValueError('File is outside approved media roots')
            return
        if self.root is not None and not resolved.is_relative_to(self.root):
            raise ValueError('File is outside WATCHED_FOLDER')

    def _is_excluded_legacy(self, path: Path) -> bool:
        resolved = path.resolve()
        if self.allowed_roots is not None:
            matching = next((r for r in self.allowed_roots if resolved.is_relative_to(r)), None)
            return matching is not None and not is_media(path, matching)
        return self.root is not None and not is_media(path, self.root)

    def submit(
        self,
        path: str | Path,
        folder_id: int | None = None,
        account_id: int | None = None,
    ) -> bool:
        path = Path(path).absolute()
        try:
            self.check_path(path)
            settings = self._effective_settings()
            signature = file_signature(path)
            resolved_account_id = account_id
            if resolved_account_id is None and folder_id is None and self.default_account_provider is not None:
                resolved_account_id = self.default_account_provider()
            with self.seen_lock:
                pending = self.store.enqueue(
                    path,
                    signature,
                    folder_id=folder_id,
                    file_size=signature[2],
                    auto_retry_failed=bool(settings['auto_retry_failed']),
                    account_id=resolved_account_id,
                )
                if pending:
                    seen = self.first_seen.get(str(path))
                    if seen is None or seen[0] != signature:
                        self.first_seen[str(path)] = (signature, time.monotonic())
                return pending
        except FileNotFoundError:
            return False  # A queued event may refer to a file already moved or removed.
        except OSError as error:
            logger.warning('File skipped until next scan file=%s error=%s', path, type(error).__name__)
            return False

    def _record_failure(
        self,
        job: dict[str, Any],
        error: Exception,
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        cfg = settings if settings is not None else self._effective_settings()
        # Upload confirmation must survive a cleanup failure; superseded entries stay intact.
        current = self.store.get(job['path'])
        if current and current['signature'] == job['signature']:
            attempts, delay, exhausted = self.store.fail(
                current, error, int(cfg['max_attempts']), float(cfg['retry_base']), float(cfg['retry_max'])
            )
            logger.warning(
                'Original retained file=%s error=%s attempts=%s retry_in=%.1fs cooldown=%s',
                job['path'], type(error).__name__, attempts, delay, exhausted,
            )
            if self.on_file_failed is not None:
                try:
                    self.on_file_failed(current, error, attempts, delay, exhausted)
                except Exception:
                    pass
        else:
            logger.info(
                'Original retained; queued entry superseded file=%s error=%s',
                job['path'], type(error).__name__,
            )

    def _finalize_confirmed_job(
        self,
        job: dict[str, Any],
        settings: Mapping[str, Any],
        *,
        duration: float | None = None,
        result_status: str = 'uploaded',
    ) -> None:
        path = Path(job['path'])
        self.check_path(path)
        if settings['delete_after_upload']:
            try:
                current_signature = file_signature(path)
            except FileNotFoundError:
                current_signature = None
            if current_signature is not None:
                if list(current_signature) != json.loads(job['signature']):
                    self.submit(path, folder_id=job.get('folder_id'), account_id=job.get('account_id'))
                    raise FileChangedError(f'File changed before cleanup: {path}')
                path.unlink()
                logger.info('Original removed file=%s', path)
        self.store.complete(
            job['path'], job['signature'], duration=duration, result_status=result_status
        )

    def _process_cleanup_job(self, job: dict[str, Any], settings: Mapping[str, Any]) -> None:
        path = Path(job['path'])
        try:
            self.check_path(path)
            if self._is_excluded_legacy(path):
                # Queued by an older version before hidden/system files were excluded.
                logger.info('Excluded file skipped file=%s', path)
                self.store.complete(job['path'], job['signature'])
                return
            logger.info('Processing file=%s attempt=%s operation=cleanup', path, job['attempts'] + 1)
            self._finalize_confirmed_job(job, settings)
        except Exception as error:
            if isinstance(error, InterruptedError) and self.stop_event.is_set():
                return
            self._record_failure(job, error, settings)

    def _collect_valid_candidates(
        self,
        upload_jobs: Sequence[dict[str, Any]],
        settings: Mapping[str, Any],
    ) -> tuple[list[BatchCandidate], int, int]:
        candidates: list[BatchCandidate] = []
        changed_candidates: list[BatchCandidate] = []
        requeued_count = 0
        failed_count = 0
        for job in upload_jobs:
            path = Path(job['path']).absolute()
            try:
                self.check_path(path)
                if self._is_excluded_legacy(path):
                    # Queued by an older version before hidden/system files were excluded.
                    logger.info('Excluded file skipped file=%s', path)
                    self.store.complete(job['path'], job['signature'])
                    continue
                current_signature = file_signature(path)
            except Exception as error:
                failed_count += 1
                self._record_failure(job, error, settings)
                continue
            if list(current_signature) != json.loads(job['signature']):
                with self.seen_lock:
                    self.first_seen.pop(str(path), None)
                self.store.enqueue(
                    job['path'],
                    current_signature,
                    folder_id=job.get('folder_id'),
                    file_size=current_signature[2],
                    account_id=job.get('account_id'),
                )
                logger.info('File changed since queued; requeued file=%s', path)
                requeued_count += 1
                updated_job = self.store.get(job['path']) or {
                    **job,
                    'signature': json.dumps(current_signature),
                    'file_size': current_signature[2],
                }
                changed_candidates.append((updated_job, path, current_signature))
                continue
            candidates.append((job, path, current_signature))
        # If other unchanged files are ready in this batch, upload them now and leave changed files requeued.
        # For a solo changed file, wait the full stability window from now rather than reusing stale first_seen.
        if not candidates and len(upload_jobs) == 1 and changed_candidates:
            return changed_candidates, 0, failed_count
        return candidates, requeued_count, failed_count

    def _execute_upload_batch(
        self,
        ready: Sequence[BatchCandidate],
        settings: Mapping[str, Any],
        client: Any,
    ) -> None:
        effective_threads = min(int(settings['upload_threads']), len(ready))
        tracker = BatchProgressTracker(batch_size=len(ready))

        def handle_progress(event: Mapping[str, Any]) -> None:
            tracker.handle_event(event)
            if self.on_progress is not None:
                try:
                    self.on_progress(event)
                except Exception:
                    pass

        if self.on_batch_start is not None:
            try:
                self.on_batch_start(ready, effective_threads)
            except Exception:
                pass

        logger.info('Starting upload batch size=%d threads=%d', len(ready), effective_threads)
        for job, path, _ in ready:
            logger.info('Processing file=%s attempt=%s operation=upload', path, job['attempts'] + 1)
        started = time.monotonic()
        # Pass a Path->None mapping so one vanished file or OS MIME gap cannot abort the whole batch.
        target: dict[Path, None] = {path: None for _, path, _ in ready}
        upload_kwargs: dict[str, Any] = {
            'target': target,
            'show_progress': False,
            'threads': effective_threads,
            'delete_from_host': False,
            'progress_callback': handle_progress,
        }
        if settings['upload_quality'] == 'saver':
            upload_kwargs['saver'] = True
        if settings['use_quota']:
            upload_kwargs['use_quota'] = True
        if not settings['duplicate_detection']:
            upload_kwargs['force_upload'] = True
        if settings['filename_conflict_policy'] == 'skip_existing_filename':
            upload_kwargs['skip_existing_filenames'] = True

        try:
            result = client.upload(**upload_kwargs)
        except Exception as error:
            if isinstance(error, InterruptedError) and self.stop_event.is_set():
                return
            duration = time.monotonic() - started
            logger.warning(
                'Batch upload failed size=%d threads=%d error=%s duration=%.2fs',
                len(ready), effective_threads, type(error).__name__, duration,
            )
            for job, _, _ in ready:
                self._record_failure(job, error, settings)
            if self.on_batch_complete is not None:
                try:
                    self.on_batch_complete({
                        'size': len(ready), 'confirmed': 0, 'failed': len(ready),
                        'deferred': 0, 'max_active': tracker.max_active, 'duration': duration,
                        'error': type(error).__name__,
                    })
                except Exception:
                    pass
            return

        batch_duration = time.monotonic() - started
        confirmed_count = 0
        failed_count = 0
        deferred_count = 0
        for job, path, stable_sig in ready:
            active_job = job
            try:
                media_key = _confirmed_media_key(result, path)
                if not media_key:
                    raise RuntimeError(f'Upload not confirmed: {path}')
                if file_signature(path) != stable_sig:
                    raise FileChangedError(f'File changed during upload; original retained: {path}')
                if not self.store.mark_uploaded(active_job, stable_sig, media_key):
                    raise FileChangedError(f'File was replaced while queued: {path}')
                active_job = {
                    **active_job,
                    'signature': json.dumps(stable_sig),
                    'media_key': media_key,
                    'file_size': stable_sig[2],
                }
                terminal = tracker.terminal_phase(path.as_posix())
                result_status = 'skipped' if terminal == 'skipped' else 'uploaded'
                logger.info(
                    'Upload confirmed file=%s status=%s duration=%.2fs',
                    path, 'duplicate' if result_status == 'skipped' else 'uploaded', batch_duration,
                )
                confirmed_count += 1
                if self.on_file_confirmed is not None:
                    try:
                        self.on_file_confirmed(active_job, media_key, result_status, batch_duration)
                    except Exception:
                        pass
                if self.stop_event.is_set():
                    deferred_count += 1
                    continue  # Keep confirmation durable; resume cleanup/completion after restart.
                self._finalize_confirmed_job(
                    active_job, settings, duration=batch_duration, result_status=result_status
                )
            except Exception as error:
                if isinstance(error, InterruptedError) and self.stop_event.is_set():
                    deferred_count += 1
                    continue
                failed_count += 1
                self._record_failure(active_job, error, settings)

        logger.info(
            'Batch completed size=%d confirmed=%d failed=%d deferred=%d max_active=%d duration=%.2fs',
            len(ready), confirmed_count, failed_count, deferred_count, tracker.max_active, batch_duration,
        )
        if self.on_batch_complete is not None:
            try:
                self.on_batch_complete({
                    'size': len(ready), 'confirmed': confirmed_count, 'failed': failed_count,
                    'deferred': deferred_count, 'max_active': tracker.max_active,
                    'duration': batch_duration,
                })
            except Exception:
                pass

    def process_once(self) -> bool:
        if self.stop_event.is_set():
            return False
        if self.is_paused is not None and self.is_paused():
            return False
        settings = self._effective_settings()
        batch_size = max(1, int(settings['upload_batch_size']))
        if self.eligible_accounts_provider is not None:
            eligible = list(self.eligible_accounts_provider())
            jobs = self.store.next_jobs(
                batch_size,
                eligible_account_ids=eligible,
                last_account_id=self._last_account_id,
            )
        else:
            jobs = self.store.next_jobs(batch_size)
        if not jobs:
            return False

        cleanup_jobs = [job for job in jobs if job['media_key']]
        upload_jobs = [job for job in jobs if not job['media_key']]
        touched_paths = [str(Path(job['path']).absolute()) for job in jobs]

        try:
            for job in cleanup_jobs:
                if self.stop_event.is_set():
                    return True
                self._process_cleanup_job(job, settings)

            if not upload_jobs or self.stop_event.is_set():
                return True

            # Guarantee strict single-account isolation within every GPMC batch call.
            batch_account_id = upload_jobs[0].get('account_id')
            if batch_account_id is not None:
                upload_jobs = [job for job in upload_jobs if job.get('account_id') == batch_account_id]
                self._last_account_id = int(batch_account_id)

            client = self._current_client(batch_account_id)
            if client is None:
                # Authentication is not ready; keep jobs pending until valid credentials are active.
                return False

            candidates, requeued_count, pre_failed = self._collect_valid_candidates(upload_jobs, settings)
            if not candidates:
                if (requeued_count > 0 and not cleanup_jobs and pre_failed == 0
                        and float(settings['stable_seconds']) > 0):
                    self.stop_event.wait(min(0.5, float(settings['stable_seconds'])))
                return True

            with self.seen_lock:
                known_map = {
                    str(path): self.first_seen[str(path)]
                    for _, path, _ in candidates
                    if str(path) in self.first_seen
                }

            try:
                ready, changed_deferred, stability_failed = wait_until_stable_batch(
                    candidates,
                    float(settings['stable_seconds']),
                    float(settings['stability_timeout']),
                    self.stop_event,
                    known_map=known_map,
                )
            except InterruptedError:
                if self.stop_event.is_set():
                    return True
                raise

            for job, error in stability_failed:
                self._record_failure(job, error, settings)
            for job, path, new_signature in changed_deferred:
                with self.seen_lock:
                    self.first_seen.pop(str(path), None)
                self.store.enqueue(
                    job['path'],
                    new_signature,
                    folder_id=job.get('folder_id'),
                    file_size=new_signature[2],
                    account_id=job.get('account_id'),
                )
                logger.info('File changed while waiting for stability; requeued file=%s', path)

            if not ready or self.stop_event.is_set():
                return True

            self._execute_upload_batch(ready, settings, client)
            return True
        finally:
            with self.seen_lock:
                for path_str in touched_paths:
                    current = self.store.get(path_str)
                    if not current or current['status'] != 'pending':
                        self.first_seen.pop(path_str, None)

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                if not self.process_once():
                    self.stop_event.wait(0.5)
        except Exception as error:
            logger.error('Worker stopped error=%s; pending work remains in SQLite', type(error).__name__)


def discover_files(root):
    def scan_error(error):
        # A missing or unreadable root must still fail the scan; one bad subfolder must not.
        if error.filename is None or os.path.abspath(error.filename) == os.path.abspath(root):
            raise error
        logger.warning('Unreadable folder skipped folder=%s error=%s', error.filename, type(error).__name__)

    for directory, directories, filenames in os.walk(root, onerror=scan_error):
        try:
            directories[:] = [name for name in directories if not name.startswith(SKIPPED_PREFIXES)
                              and not (Path(directory) / name).is_symlink()]
            media = [path for name in filenames if is_media(path := Path(directory) / name, root)]
        except OSError as error:
            # Listable but not searchable: entries exist but their metadata cannot be read.
            if os.path.abspath(directory) == os.path.abspath(root):
                raise
            logger.warning('Unreadable folder skipped folder=%s error=%s', directory, type(error).__name__)
            directories[:] = []
            continue
        yield from (path.absolute() for path in media)


class PhotoHandler(FileSystemEventHandler):
    def __init__(self, submit, watched_folder):
        self.submit = submit
        self.watched_folder = Path(watched_folder).absolute()  # same form as event paths
        self.root = self.watched_folder.resolve()

    def submit_path(self, path):
        try:
            path = Path(path).absolute()
            if path.resolve().is_relative_to(self.root) and is_media(path, self.watched_folder):
                self.submit(path)
        except (OSError, ValueError) as error:
            logger.warning('File event deferred to reconciliation error=%s', type(error).__name__)

    def on_created(self, event):
        if not event.is_directory:
            self.submit_path(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.submit_path(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.submit_path(event.src_path)


class WatchSupervisor:
    def __init__(self, root: str | Path, submit: Callable[[Path], Any], poll_interval: float = 1, recursive: bool = True) -> None:
        self.root = Path(root)
        self.submit = submit
        self.recursive = recursive
        self.handler = PhotoHandler(lambda path: self.submit(path), root)
        self.poll_interval = poll_interval
        self.observer = None
        self.identity = None
        self.generation = 0
        self.last_error = None

    def stop(self):
        if self.observer is not None:
            self.observer.stop()
            if self.observer.ident is not None:
                self.observer.join()
            self.observer = None

    def check(self):
        try:
            info = self.root.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError(str(self.root))
            with os.scandir(self.root) as entries:
                next(entries, None)
            identity = (info.st_dev, info.st_ino)
            if (self.observer is not None and self.observer.is_alive()
                    and self.observer.emitters
                    and all(emitter.is_alive() for emitter in self.observer.emitters)
                    and identity == self.identity):
                return True
            self.stop()
            self.observer = Observer(timeout=self.poll_interval)
            self.observer.schedule(self.handler, str(self.root), recursive=self.recursive)
            self.observer.start()
            self.identity = identity
            self.generation += 1
            self.last_error = None
            logger.info('Monitoring active folder=%s recursive=%s', self.root, self.recursive)
            return True
        except OSError as error:
            self.stop()
            if self.last_error != type(error).__name__:
                self.last_error = type(error).__name__
                logger.warning('Folder unavailable; reconnecting error=%s', self.last_error)
            return False


def write_health(state_dir, healthy):
    path = Path(state_dir) / 'health.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'ok': healthy, 'updated_at': time.time()}), encoding='utf-8')
    temporary.replace(path)


def healthcheck(state_dir, max_age=30):
    try:
        data = json.loads((Path(state_dir) / 'health.json').read_text(encoding='utf-8'))
        age = time.time() - data['updated_at']
        return data['ok'] is True and 0 <= age <= max_age
    except (OSError, ValueError, KeyError, TypeError):
        return False


def run_service(config, client_factory, stop_event):
    watched_folder = config.root
    state_dir = config.state_dir
    write_health(state_dir, False)
    client = client_factory(auth_data=config.auth_data, timeout=config.request_timeout,
                            log_level=config.log_level)
    store = StateStore(state_dir / 'uploads.db', scope=config.state_scope)
    processor = FileProcessor(
        client, store, delete_after_upload=config.delete_after_upload,
        stop_event=stop_event, root=watched_folder,
        stable_seconds=config.stable_seconds, stability_timeout=config.stability_timeout,
        max_attempts=config.max_attempts, retry_base=config.retry_base, retry_max=config.retry_max,
        upload_threads=config.upload_threads, upload_batch_size=config.upload_batch_size)
    worker = Thread(target=processor.run, name='upload-worker', daemon=True)
    supervisor = WatchSupervisor(watched_folder, processor.submit, config.poll_interval)
    next_scan = 0
    try:
        worker.start()
        logger.info(
            'Service started folder=%s delete_after_upload=%s upload_threads=%s upload_batch_size=%s',
            watched_folder, config.delete_after_upload, config.upload_threads, config.upload_batch_size,
        )
        while not processor.stop_event.is_set():
            if not worker.is_alive():
                raise RuntimeError('Upload worker stopped unexpectedly')
            generation = supervisor.generation
            healthy = supervisor.check()
            if supervisor.generation != generation:
                next_scan = 0
            if healthy and time.monotonic() >= next_scan:
                try:
                    heartbeat = time.monotonic()
                    for path in discover_files(watched_folder):
                        if processor.stop_event.is_set():
                            break
                        processor.submit(path)
                        if time.monotonic() - heartbeat >= 5:
                            write_health(state_dir, worker.is_alive())
                            heartbeat = time.monotonic()
                    next_scan = time.monotonic() + config.scan_interval
                except OSError as error:
                    logger.warning('Reconciliation interrupted error=%s', type(error).__name__)
                    healthy = False
                    next_scan = 0
            write_health(state_dir, healthy and worker.is_alive())
            processor.stop_event.wait(5)
    finally:
        processor.stop_event.set()
        try:
            write_health(state_dir, False)
        except OSError:
            logger.error('Could not update health during shutdown')
        deadline = time.monotonic() + config.shutdown_timeout
        monitor_stop = Thread(target=supervisor.stop, name='monitor-shutdown', daemon=True)
        monitor_stop.start()
        monitor_stop.join(timeout=max(0, deadline - time.monotonic()))
        if worker.ident is not None:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        if worker.is_alive() or monitor_stop.is_alive():
            raise ShutdownTimeout('Shutdown deadline exceeded; pending work remains durable')
        store.close()
        logger.info('Service stopped; pending work saved')


def main():
    try:
        config = Config.from_env()
    except (ValueError, OSError) as error:
        logging.basicConfig(level=logging.ERROR)
        logger.error('Configuration error: %s', error)
        return 2
    configure_logging(config)
    if config.dashboard_enabled:
        from api_server import run_dashboard_server

        return run_dashboard_server(config)

    # GPMC changes SIGINT when imported; install our handlers afterwards.
    from gpmc import Client

    stop_event = Event()
    previous = install_signal_handlers(stop_event)
    try:
        run_service(config, Client, stop_event)
        return 0
    except ShutdownTimeout:
        logger.error('Shutdown timed out; exiting with durable pending work')
        logging.shutdown()
        # GPMC's executor threads can otherwise keep the interpreter alive.
        os._exit(1)
    except Exception as error:
        logger.error('Service failed error=%s', type(error).__name__)
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    if sys.argv[1:] == ['--healthcheck']:
        sys.exit(0 if healthcheck(os.environ.get('STATE_DIR', '/state')) else 1)
    sys.exit(main())
