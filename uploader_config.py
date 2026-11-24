"""Validate configuration before initializing the client or background threads."""

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

APP_VERSION = '2.0.0'
MIN_UPLOAD_THREADS = 1
MAX_UPLOAD_THREADS = 8
MIN_UPLOAD_BATCH_SIZE = 1
MAX_UPLOAD_BATCH_SIZE = 64
REQUIRED_AUTH_FIELDS = frozenset({
    'androidId', 'client_sig', 'callerSig', 'device_country', 'Email',
    'google_play_services_version', 'lang', 'oauth2_foreground',
    'sdk_version', 'service', 'Token',
})
AUTH_OVERRIDE_FILENAME = 'auth_override.secret'


def validate_auth_data_structure(auth: str) -> dict[str, list[str]]:
    fields = parse_qs(auth.strip(), strict_parsing=False)
    if not REQUIRED_AUTH_FIELDS.issubset(fields):
        raise ValueError(
            'Provide complete AUTH_DATA or AUTH_DATA_FILE from the captured authentication request'
        )
    for key in REQUIRED_AUTH_FIELDS:
        if not fields.get(key) or not fields[key][0].strip():
            raise ValueError(
                'Provide complete AUTH_DATA or AUTH_DATA_FILE from the captured authentication request'
            )
    return fields


def parse_bool(env: Mapping[str, Any], name: str, default: bool = False) -> bool:
    raw = str(env.get(name, 'true' if default else 'false')).lower().strip()
    if raw not in ('true', 'false'):
        raise ValueError(f'{name} must be true or false')
    return raw == 'true'


def number(env: Mapping[str, Any], name: str, default: int | float, integer: bool = False) -> int | float:
    try:
        raw = env.get(name, default)
        if isinstance(raw, bool):
            raise ValueError
        value = int(raw) if integer else float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ValueError(f'{name} must be a positive {"integer" if integer else "number"}') from None


def parse_approved_roots(env: Mapping[str, Any], default_root: Path) -> tuple[Path, ...]:
    raw = str(env.get('APPROVED_MEDIA_ROOTS', '')).strip()
    roots: list[Path] = []
    seen: set[Path] = set()
    candidates = [item.strip() for item in raw.split(',') if item.strip()] if raw else []
    if str(default_root) not in candidates:
        candidates.insert(0, str(default_root))
    for item in candidates:
        resolved = Path(item).resolve()
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)
    return tuple(roots)


@dataclass(frozen=True)
class Config:
    root: Path
    state_dir: Path
    auth_data: str = field(repr=False)
    delete_after_upload: bool = False
    stable_seconds: float = 5
    stability_timeout: float = 300
    scan_interval: float = 60
    poll_interval: float = 30
    request_timeout: int = 60
    max_attempts: int = 5
    retry_base: float = 5
    retry_max: float = 3600
    shutdown_timeout: float = 120
    upload_threads: int = 2
    upload_batch_size: int = 4
    upload_quality: str = 'original'
    use_quota: bool = False
    duplicate_detection: bool = True
    filename_conflict_policy: str = 'hash_dedup'
    auto_retry_failed: bool = True
    approved_roots: tuple[Path, ...] = ()
    auth_data_file: str = ''
    auth_source: str = 'env'
    dashboard_enabled: bool = False
    dashboard_host: str = '0.0.0.0'
    dashboard_port: int = 8080
    dashboard_username: str = 'admin'
    dashboard_password: str = field(default='', repr=False)
    dashboard_session_secret: str = field(default='', repr=False)
    dashboard_secure_cookies: bool = False
    immich_url: str = ''
    immich_public_url: str = ''
    immich_media_root: str = ''
    immich_api_key: str = field(default='', repr=False)
    log_level: str = 'INFO'

    @property
    def state_scope(self) -> str | None:
        if not self.auth_data:
            return None
        # Refreshing a token preserves state; switching account/root/deletion mode
        # requires new remote confirmation before considering any source complete.
        scope = {'account': parse_qs(self.auth_data)['Email'][0].casefold(),
                 'root': str(self.root), 'delete': self.delete_after_upload, 'version': 1}
        return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_env(cls, env: Mapping[str, Any] | None = None) -> 'Config':
        env = os.environ if env is None else env
        root_value = env.get('WATCHED_FOLDER', '/data').strip()
        if not root_value:
            raise ValueError('WATCHED_FOLDER cannot be empty')
        root = Path(root_value).resolve()
        state_value = env.get('STATE_DIR', '/state').strip()
        if not state_value:
            raise ValueError('STATE_DIR cannot be empty')
        state_dir = Path(state_value).resolve()
        immich_media_root = str(env.get('IMMICH_MEDIA_ROOT', '')).strip()
        approved_roots = parse_approved_roots(env, root)
        if immich_media_root:
            resolved_immich = Path(immich_media_root).resolve()
            if resolved_immich not in approved_roots:
                approved_roots = (*approved_roots, resolved_immich)
        for approved in approved_roots:
            if state_dir.is_relative_to(approved) or approved.is_relative_to(state_dir):
                raise ValueError('STATE_DIR and WATCHED_FOLDER must be separate directories')

        raw_delete = env.get('DELETE_AFTER_UPLOAD', 'false').lower().strip()
        if raw_delete not in ('true', 'false'):
            raise ValueError('DELETE_AFTER_UPLOAD must be true or false')

        auth = env.get('AUTH_DATA', '').strip() or env.get('GP_AUTH_DATA', '').strip()
        auth_file = env.get('AUTH_DATA_FILE', '').strip()
        if auth and auth_file:
            raise ValueError('Set either AUTH_DATA or AUTH_DATA_FILE, not both')
        auth_source = 'env'
        override_path = state_dir / AUTH_OVERRIDE_FILENAME
        if override_path.is_file():
            try:
                override_content = override_path.read_text(encoding='utf-8').strip()
                if override_content:
                    auth = override_content
                    auth_source = 'managed_override'
            except (OSError, UnicodeError):
                raise ValueError('Managed authentication override cannot be read') from None
        elif auth_file:
            auth_source = 'secret_file'
            try:
                auth = Path(auth_file).read_text(encoding='utf-8').strip()
            except (OSError, UnicodeError):
                raise ValueError('AUTH_DATA_FILE cannot be read') from None

        allow_unconfigured = parse_bool(env, 'ALLOW_UNCONFIGURED_AUTH', False)
        if auth:
            validate_auth_data_structure(auth)
        elif not allow_unconfigured:
            raise ValueError(
                'Provide complete AUTH_DATA or AUTH_DATA_FILE from the captured authentication request'
            )
        else:
            auth_source = 'none'

        if root.exists():
            if not root.is_dir():
                raise ValueError('WATCHED_FOLDER must be a directory')
            if not os.access(root, os.R_OK | os.X_OK):
                raise ValueError('WATCHED_FOLDER must be readable and searchable')
            if raw_delete == 'true' and not os.access(root, os.W_OK):
                raise ValueError('WATCHED_FOLDER must be writable when deletion is enabled')
        log_level = env.get('LOG_LEVEL', 'INFO').upper()
        if log_level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
            raise ValueError('LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL')
        upload_quality = str(env.get('UPLOAD_QUALITY', 'original')).lower().strip()
        if upload_quality not in ('original', 'saver'):
            raise ValueError('UPLOAD_QUALITY must be original or saver')
        conflict_policy = str(env.get('FILENAME_CONFLICT_POLICY', 'hash_dedup')).lower().strip()
        if conflict_policy not in ('hash_dedup', 'skip_existing_filename'):
            raise ValueError('FILENAME_CONFLICT_POLICY must be hash_dedup or skip_existing_filename')
        dashboard_port = int(number(env, 'DASHBOARD_PORT', 8080, integer=True))
        if not 1 <= dashboard_port <= 65535:
            raise ValueError('DASHBOARD_PORT must be between 1 and 65535')
        dashboard_username = str(env.get('DASHBOARD_USERNAME', 'admin')).strip()
        if not dashboard_username:
            raise ValueError('DASHBOARD_USERNAME cannot be empty')

        config = cls(
            root=root, state_dir=state_dir, auth_data=auth,
            delete_after_upload=raw_delete == 'true', log_level=log_level,
            stable_seconds=number(env, 'STABLE_SECONDS', 5),
            stability_timeout=number(env, 'STABILITY_TIMEOUT', 300),
            scan_interval=number(env, 'SCAN_INTERVAL', 60),
            poll_interval=number(env, 'POLL_INTERVAL', 30),
            request_timeout=int(number(env, 'REQUEST_TIMEOUT', 60, integer=True)),
            max_attempts=int(number(env, 'MAX_ATTEMPTS', 5, integer=True)),
            retry_base=number(env, 'RETRY_BASE', 5),
            retry_max=number(env, 'RETRY_MAX', 3600),
            shutdown_timeout=number(env, 'SHUTDOWN_TIMEOUT', 120),
            upload_threads=int(number(env, 'UPLOAD_THREADS', 2, integer=True)),
            upload_batch_size=int(number(env, 'UPLOAD_BATCH_SIZE', 4, integer=True)),
            upload_quality=upload_quality,
            use_quota=parse_bool(env, 'USE_QUOTA', False),
            duplicate_detection=parse_bool(env, 'DUPLICATE_DETECTION', True),
            filename_conflict_policy=conflict_policy,
            auto_retry_failed=parse_bool(env, 'AUTO_RETRY_FAILED', True),
            approved_roots=approved_roots,
            auth_data_file=auth_file,
            auth_source=auth_source,
            dashboard_enabled=parse_bool(env, 'DASHBOARD_ENABLED', False),
            dashboard_host=str(env.get('DASHBOARD_HOST', '0.0.0.0')).strip() or '0.0.0.0',
            dashboard_port=dashboard_port,
            dashboard_username=dashboard_username,
            dashboard_password=str(env.get('DASHBOARD_PASSWORD', '')),
            dashboard_session_secret=str(env.get('DASHBOARD_SESSION_SECRET', '')),
            dashboard_secure_cookies=parse_bool(env, 'DASHBOARD_SECURE_COOKIES', False),
            immich_url=str(env.get('IMMICH_URL', '')).strip().rstrip('/'),
            immich_public_url=str(env.get('IMMICH_PUBLIC_URL', '')).strip().rstrip('/'),
            immich_media_root=str(Path(immich_media_root).resolve()) if immich_media_root else '',
            immich_api_key=str(env.get('IMMICH_API_KEY', '')).strip(),
        )
        if config.stability_timeout < config.stable_seconds:
            raise ValueError('STABILITY_TIMEOUT must be at least STABLE_SECONDS')
        if config.retry_max < config.retry_base:
            raise ValueError('RETRY_MAX must be at least RETRY_BASE')
        if not MIN_UPLOAD_THREADS <= config.upload_threads <= MAX_UPLOAD_THREADS:
            raise ValueError(
                f'UPLOAD_THREADS must be between {MIN_UPLOAD_THREADS} and {MAX_UPLOAD_THREADS}'
            )
        if not MIN_UPLOAD_BATCH_SIZE <= config.upload_batch_size <= MAX_UPLOAD_BATCH_SIZE:
            raise ValueError(
                f'UPLOAD_BATCH_SIZE must be between {MIN_UPLOAD_BATCH_SIZE} and {MAX_UPLOAD_BATCH_SIZE}'
            )
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=state_dir) as probe:
                probe.write(b'storage check')
                probe.flush()
        except OSError:
            raise ValueError('STATE_DIR must be a writable directory on local storage') from None
        return config
