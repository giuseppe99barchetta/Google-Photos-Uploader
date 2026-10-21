"""Validate configuration before initializing the client or background threads."""

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs


def number(env, name, default, integer=False):
    try:
        value = int(env.get(name, default)) if integer else float(env.get(name, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ValueError(f'{name} must be a positive {"integer" if integer else "number"}') from None


@dataclass(frozen=True)
class Config:
    root: Path
    state_dir: Path
    auth_data: str = field(repr=False)
    delete_after_upload: bool = False
    stable_seconds: float = 5
    stability_timeout: float = 300
    scan_interval: float = 60
    poll_interval: float = 1
    request_timeout: int = 60
    max_attempts: int = 5
    retry_base: float = 5
    retry_max: float = 3600
    shutdown_timeout: float = 120
    log_level: str = 'INFO'

    @property
    def state_scope(self):
        # Refreshing a token preserves state; switching account/root/deletion mode
        # requires new remote confirmation before considering any source complete.
        scope = {'account': parse_qs(self.auth_data)['Email'][0].casefold(),
                 'root': str(self.root), 'delete': self.delete_after_upload, 'version': 1}
        return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        auth = env.get('AUTH_DATA', '').strip() or env.get('GP_AUTH_DATA', '').strip()
        auth_file = env.get('AUTH_DATA_FILE', '').strip()
        if auth and auth_file:
            raise ValueError('Set either AUTH_DATA or AUTH_DATA_FILE, not both')
        if auth_file:
            try:
                auth = Path(auth_file).read_text(encoding='utf-8').strip()
            except (OSError, UnicodeError):
                raise ValueError('AUTH_DATA_FILE cannot be read') from None
        fields = parse_qs(auth)
        required = {'androidId', 'client_sig', 'callerSig', 'device_country', 'Email',
                    'google_play_services_version', 'lang', 'oauth2_foreground',
                    'sdk_version', 'service', 'Token'}
        if not required.issubset(fields):
            raise ValueError('Provide complete AUTH_DATA or AUTH_DATA_FILE from the captured authentication request')
        raw_delete = env.get('DELETE_AFTER_UPLOAD', 'false').lower().strip()
        if raw_delete not in ('true', 'false'):
            raise ValueError('DELETE_AFTER_UPLOAD must be true or false')
        root_value = env.get('WATCHED_FOLDER', '/data').strip()
        if not root_value:
            raise ValueError('WATCHED_FOLDER cannot be empty')
        root = Path(root_value).resolve()
        state_value = env.get('STATE_DIR', '/state').strip()
        if not state_value:
            raise ValueError('STATE_DIR cannot be empty')
        state_dir = Path(state_value).resolve()
        if state_dir.is_relative_to(root) or root.is_relative_to(state_dir):
            raise ValueError('STATE_DIR and WATCHED_FOLDER must be separate directories')
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
        config = cls(
            root=root, state_dir=state_dir, auth_data=auth,
            delete_after_upload=raw_delete == 'true', log_level=log_level,
            stable_seconds=number(env, 'STABLE_SECONDS', 5),
            stability_timeout=number(env, 'STABILITY_TIMEOUT', 300),
            scan_interval=number(env, 'SCAN_INTERVAL', 60),
            poll_interval=number(env, 'POLL_INTERVAL', 1),
            request_timeout=number(env, 'REQUEST_TIMEOUT', 60, integer=True),
            max_attempts=number(env, 'MAX_ATTEMPTS', 5, integer=True),
            retry_base=number(env, 'RETRY_BASE', 5),
            retry_max=number(env, 'RETRY_MAX', 3600),
            shutdown_timeout=number(env, 'SHUTDOWN_TIMEOUT', 120),
        )
        if config.stability_timeout < config.stable_seconds:
            raise ValueError('STABILITY_TIMEOUT must be at least STABLE_SECONDS')
        if config.retry_max < config.retry_base:
            raise ValueError('RETRY_MAX must be at least RETRY_BASE')
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=state_dir) as probe:
                probe.write(b'storage check')
                probe.flush()
        except OSError:
            raise ValueError('STATE_DIR must be a writable directory on local storage') from None
        return config
