"""Immich server integration: secure API key storage, user discovery, and per-user folder binding."""

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse

import requests

from folder_manager import validate_approved_path
from script_gphoto import register_redacted_secret
from uploader_config import Config

logger = logging.getLogger('gphotos_uploader')
IMMICH_CONFIG_FILENAME = 'immich_config.json'
IMMICH_KEY_FILENAME = 'immich_api.secret'
IMMICH_SYSTEM_DIRS = frozenset({'thumbs', 'encoded-video', 'profile', 'backups', 'library', 'upload'})
SAFE_STORAGE_LABEL_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$')


def validate_immich_url(raw_url: str) -> str:
    cleaned = raw_url.strip().rstrip('/')
    if not cleaned:
        return ''
    parsed = urlparse(cleaned)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Immich URL must be a valid http:// or https:// address')
    if parsed.username or parsed.password:
        raise ValueError('Immich URL must not embed credentials')
    return cleaned


def _write_atomic_secret(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix='.immich_key_', dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content.strip() + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class ImmichManager:
    """Connects to an Immich instance safely and resolves per-user media directories."""

    def __init__(
        self,
        config: Config,
        approved_roots: Sequence[Path],
        http_request: Callable[..., requests.Response] | None = None,
    ) -> None:
        self.state_dir = config.state_dir
        self.approved_roots = tuple(r.resolve() for r in approved_roots)
        self._http_request = http_request or requests.request
        self._lock = RLock()
        self._config_path = self.state_dir / IMMICH_CONFIG_FILENAME
        self._secret_path = self.state_dir / IMMICH_KEY_FILENAME

        self._url = config.immich_url
        self._public_url = config.immich_public_url
        self._media_root = config.immich_media_root
        self._api_key = config.immich_api_key
        self._load_persisted()

        if self._api_key:
            register_redacted_secret(self._api_key)

    def _load_persisted(self) -> None:
        if self._config_path.is_file():
            try:
                data = json.loads(self._config_path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    if data.get('url'):
                        self._url = str(data['url']).strip().rstrip('/')
                    if data.get('public_url'):
                        self._public_url = str(data['public_url']).strip().rstrip('/')
                    if data.get('media_root'):
                        self._media_root = str(data['media_root']).strip()
            except (OSError, ValueError):
                pass
        if self._secret_path.is_file():
            try:
                stored_key = self._secret_path.read_text(encoding='utf-8').strip()
                if stored_key:
                    self._api_key = stored_key
            except (OSError, UnicodeError):
                pass

    def _save_config_unlocked(self) -> None:
        payload = {
            'url': self._url,
            'public_url': self._public_url,
            'media_root': self._media_root,
        }
        tmp = self._config_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload), encoding='utf-8')
        tmp.replace(self._config_path)

    def _resolved_media_root(self) -> Path | None:
        raw = self._media_root
        if not raw:
            # Auto-detect /media/immich if mounted inside approved roots.
            for root in self.approved_roots:
                if root.name.lower() == 'immich':
                    return root
            return None
        try:
            return validate_approved_path(raw, self.approved_roots, require_exists=False)
        except (PermissionError, ValueError):
            return None

    def configure(
        self,
        *,
        url: str | None = None,
        public_url: str | None = None,
        media_root: str | None = None,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if url is not None:
                self._url = validate_immich_url(url)
            if public_url is not None:
                self._public_url = validate_immich_url(public_url)
            if media_root is not None:
                cleaned_root = media_root.strip()
                if cleaned_root:
                    resolved = validate_approved_path(cleaned_root, self.approved_roots, require_exists=False)
                    self._media_root = str(resolved)
                else:
                    self._media_root = ''
            if clear_api_key:
                self._api_key = ''
                self._secret_path.unlink(missing_ok=True)
            elif api_key is not None and api_key.strip():
                cleaned_key = api_key.strip()
                register_redacted_secret(cleaned_key)
                if 'androidId=' in cleaned_key or 'Token=' in cleaned_key:
                    raise ValueError(
                        'That looks like Google Android AUTH_DATA. Paste it under Google Accounts instead; '
                        'here, paste the short API key created in Immich -> Account Settings -> API Keys.'
                    )
                if any(ch.isspace() for ch in cleaned_key):
                    raise ValueError(
                        'Immich API key cannot contain spaces or line breaks. Copy only the key string from '
                        'Immich -> Account Settings -> API Keys.'
                    )
                # If Immich is reachable, verify the key before persisting so a bad key cannot break setup.
                if self._url:
                    try:
                        ping = self._api_call('GET', '/server/ping', api_key='', timeout=4.0)
                        if ping.status_code == 200 and ping.json().get('res') == 'pong':
                            verify = self._api_call('GET', '/users/me', api_key=cleaned_key, timeout=6.0)
                            if verify.status_code == 401:
                                raise ValueError(
                                    'Immich rejected this API key (HTTP 401). Create a new API key in '
                                    'Immich -> Account Settings -> API Keys and paste it here.'
                                )
                    except ValueError:
                        raise
                    except Exception:
                        pass
                _write_atomic_secret(self._secret_path, cleaned_key)
                self._api_key = cleaned_key
            self._save_config_unlocked()
        return self.get_status()

    def _api_call(
        self,
        method: str,
        endpoint: str,
        *,
        api_key: str | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 8.0,
    ) -> requests.Response:
        with self._lock:
            base_url = self._url
            key = api_key if api_key is not None else self._api_key
        if not base_url:
            raise ValueError('Immich server URL is not configured')
        headers: dict[str, str] = {'Accept': 'application/json'}
        if key:
            headers['x-api-key'] = key
        return self._http_request(
            method,
            f'{base_url}/api{endpoint}',
            headers=headers,
            json=json_body,
            timeout=timeout,
        )

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            url = self._url
            public_url = self._public_url or (url if url and 'immich-server' not in url else 'http://localhost:2283')
            has_key = bool(self._api_key)
            media_root = self._resolved_media_root()

        media_root_exists = bool(media_root and media_root.is_dir())
        media_root_readable = bool(media_root_exists and media_root and os.access(media_root, os.R_OK | os.X_OK))
        has_upload_dir = bool(media_root_exists and media_root and (media_root / 'upload').is_dir())
        has_library_dir = bool(media_root_exists and media_root and (media_root / 'library').is_dir())

        reachable = False
        version: str | None = None
        api_key_valid: bool | None = None
        admin_access = False
        error: str | None = None

        if url:
            try:
                ping = self._api_call('GET', '/server/ping', api_key='', timeout=4.0)
                if ping.status_code == 200 and ping.json().get('res') == 'pong':
                    reachable = True
                    try:
                        ver_resp = self._api_call('GET', '/server/version', api_key='', timeout=4.0)
                        if ver_resp.status_code == 200:
                            v = ver_resp.json()
                            version = f'v{v.get("major", 0)}.{v.get("minor", 0)}.{v.get("patch", 0)}'
                    except Exception:
                        pass
                else:
                    error = f'Unexpected ping status ({ping.status_code})'
            except Exception as exc:
                error = f'Cannot reach Immich at {url} ({type(exc).__name__})'

        if reachable and has_key:
            try:
                admin_resp = self._api_call('GET', '/admin/users', timeout=6.0)
                if admin_resp.status_code == 200:
                    api_key_valid = True
                    admin_access = True
                elif admin_resp.status_code == 403:
                    user_resp = self._api_call('GET', '/users', timeout=6.0)
                    api_key_valid = user_resp.status_code == 200
                    admin_access = False
                elif admin_resp.status_code == 401:
                    api_key_valid = False
                    error = 'Immich API key was rejected (HTTP 401)'
                else:
                    api_key_valid = False
                    error = f'Immich API returned HTTP {admin_resp.status_code}'
            except Exception as exc:
                api_key_valid = False
                error = f'Failed verifying Immich API key ({type(exc).__name__})'

        return {
            'configured': bool(url),
            'url': url,
            'public_url': public_url,
            'reachable': reachable,
            'version': version,
            'has_api_key': has_key,
            'api_key_valid': api_key_valid,
            'admin_access': admin_access,
            'media_root': str(media_root) if media_root else '',
            'media_root_exists': media_root_exists,
            'media_root_readable': media_root_readable,
            'has_upload_dir': has_upload_dir,
            'has_library_dir': has_library_dir,
            'error': error,
        }

    def resolve_user_paths(self, user_id: str, storage_label: str | None = None) -> dict[str, Any]:
        media_root = self._resolved_media_root()
        if media_root is None:
            return {'recommended_path': None, 'exists': False, 'storage_mode': None, 'candidates': []}

        candidates: list[dict[str, Any]] = []
        clean_label = (storage_label or '').strip()
        if clean_label and SAFE_STORAGE_LABEL_RE.match(clean_label):
            p = media_root / 'library' / clean_label
            candidates.append({'path': str(p), 'mode': 'library', 'exists': p.is_dir()})
        clean_id = user_id.strip()
        if clean_id and SAFE_STORAGE_LABEL_RE.match(clean_id):
            lib_p = media_root / 'library' / clean_id
            up_p = media_root / 'upload' / clean_id
            candidates.append({'path': str(lib_p), 'mode': 'library', 'exists': lib_p.is_dir()})
            candidates.append({'path': str(up_p), 'mode': 'upload', 'exists': up_p.is_dir()})

        # Prefer an existing directory on disk; otherwise prefer library/<label> if library/ exists, else upload/<id>.
        chosen = next((c for c in candidates if c['exists']), None)
        if chosen is None and candidates:
            if clean_label and (media_root / 'library').is_dir():
                chosen = candidates[0]
            else:
                chosen = next((c for c in candidates if c['mode'] == 'upload'), candidates[0])

        return {
            'recommended_path': chosen['path'] if chosen else None,
            'exists': bool(chosen and chosen['exists']),
            'storage_mode': chosen['mode'] if chosen else None,
            'candidates': candidates,
        }

    def discover_disk_folders(self) -> list[dict[str, Any]]:
        """Discover per-user Immich directories directly on disk (works even without an API key)."""
        media_root = self._resolved_media_root()
        if media_root is None or not media_root.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for mode in ('library', 'upload'):
            parent = media_root / mode
            if not parent.is_dir() or parent.is_symlink():
                continue
            try:
                entries = sorted(parent.iterdir(), key=lambda item: item.name.lower())
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                if entry.name.lower() in IMMICH_SYSTEM_DIRS or entry.name.startswith('.'):
                    continue
                resolved = entry.resolve()
                if not resolved.is_relative_to(media_root):
                    continue
                found.append({
                    'label': entry.name,
                    'path': str(resolved),
                    'storage_mode': mode,
                })
        return found

    def list_users(self) -> dict[str, Any]:
        with self._lock:
            url = self._url
            has_key = bool(self._api_key)
        disk_folders = self.discover_disk_folders()
        if not url or not has_key:
            return {
                'users': [],
                'disk_folders': disk_folders,
                'admin_access': False,
                'api_connected': False,
                'message': 'Connect an Immich Admin API key to list users automatically, or bind discovered disk folders below.',
            }

        admin_access = False
        try:
            resp = self._api_call('GET', '/admin/users', timeout=8.0)
            if resp.status_code == 200:
                raw_users = resp.json()
                admin_access = True
            elif resp.status_code == 403:
                resp = self._api_call('GET', '/users', timeout=8.0)
                if resp.status_code != 200:
                    return {
                        'users': [],
                        'disk_folders': disk_folders,
                        'admin_access': False,
                        'api_connected': False,
                        'message': f'Immich returned HTTP {resp.status_code} when listing users.',
                    }
                raw_users = resp.json()
            elif resp.status_code == 401:
                return {
                    'users': [],
                    'disk_folders': disk_folders,
                    'admin_access': False,
                    'api_connected': False,
                    'message': (
                        'Immich rejected the stored API key (HTTP 401). Click Clear Key and paste a valid '
                        'API key from Immich -> Account Settings -> API Keys.'
                    ),
                }
            else:
                return {
                    'users': [],
                    'disk_folders': disk_folders,
                    'admin_access': False,
                    'api_connected': False,
                    'message': f'Immich returned HTTP {resp.status_code} when listing users.',
                }
        except Exception as exc:
            return {
                'users': [],
                'disk_folders': disk_folders,
                'admin_access': False,
                'api_connected': False,
                'message': f'Could not reach Immich server ({type(exc).__name__}).',
            }

        users: list[dict[str, Any]] = []
        for raw in raw_users if isinstance(raw_users, list) else []:
            uid = str(raw.get('id') or '').strip()
            if not uid:
                continue
            storage_label = raw.get('storageLabel')
            resolved = self.resolve_user_paths(uid, storage_label)
            users.append({
                'id': uid,
                'name': str(raw.get('name') or raw.get('email') or uid),
                'email': str(raw.get('email') or ''),
                'is_admin': bool(raw.get('isAdmin', False)),
                'status': str(raw.get('status') or 'active'),
                'storage_label': storage_label,
                'quota_usage_bytes': raw.get('quotaUsageInBytes'),
                'recommended_path': resolved['recommended_path'],
                'folder_exists': resolved['exists'],
                'storage_mode': resolved['storage_mode'],
                'candidate_paths': resolved['candidates'],
            })
        return {
            'users': users,
            'disk_folders': disk_folders,
            'admin_access': admin_access,
            'api_connected': True,
            'message': None,
        }

    def create_user(
        self,
        *,
        name: str,
        email: str,
        password: str,
        storage_label: str | None = None,
        is_admin: bool = False,
    ) -> dict[str, Any]:
        cleaned_name = name.strip()
        cleaned_email = email.strip()
        if not cleaned_name or not cleaned_email or '@' not in cleaned_email:
            raise ValueError('Valid name and email are required to create an Immich user')
        if len(password) < 6:
            raise ValueError('Immich user password must be at least 6 characters')
        payload: dict[str, Any] = {
            'name': cleaned_name,
            'email': cleaned_email,
            'password': password,
            'isAdmin': bool(is_admin),
            'shouldChangePassword': False,
        }
        if storage_label and storage_label.strip():
            label = storage_label.strip()
            if not SAFE_STORAGE_LABEL_RE.match(label):
                raise ValueError('Storage label must contain only letters, numbers, dots, underscores, or hyphens')
            payload['storageLabel'] = label

        resp = self._api_call('POST', '/admin/users', json_body=payload, timeout=10.0)
        if resp.status_code in (200, 201):
            raw = resp.json()
            uid = str(raw.get('id') or '')
            resolved = self.resolve_user_paths(uid, raw.get('storageLabel'))
            return {
                'id': uid,
                'name': str(raw.get('name') or cleaned_name),
                'email': str(raw.get('email') or cleaned_email),
                'is_admin': bool(raw.get('isAdmin', False)),
                'storage_label': raw.get('storageLabel'),
                'recommended_path': resolved['recommended_path'],
                'folder_exists': resolved['exists'],
                'storage_mode': resolved['storage_mode'],
                'candidate_paths': resolved['candidates'],
            }
        if resp.status_code in (401, 403):
            raise PermissionError('Admin API key is required to create Immich users')
        detail = ''
        try:
            body = resp.json()
            detail = str(body.get('message') or '')
        except Exception:
            pass
        raise ValueError(detail or f'Immich rejected user creation (HTTP {resp.status_code})')
