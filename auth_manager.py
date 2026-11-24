"""Secure multi-account Android AUTH_DATA validation, atomic 0600 secret storage, and client reloading."""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qs

from script_gphoto import register_redacted_secret
from upload_state import StateStore
from uploader_config import AUTH_OVERRIDE_FILENAME, Config, validate_auth_data_structure

logger = logging.getLogger('gphotos_uploader')
AUTH_STATUS_FILENAME = 'auth_status.json'
ACCOUNTS_SECRET_DIR = 'accounts'
GPMC_CACHE_DIR = 'gpmc_cache'


def mask_email(email: str) -> str:
    email = email.strip()
    if '@' not in email:
        return '***' if email else ''
    local, domain = email.split('@', 1)
    if len(local) <= 2:
        masked_local = f'{local[:1]}***'
    else:
        masked_local = f'{local[0]}***{local[-1]}'
    return f'{masked_local}@{domain}'


def hash_email(email: str) -> str:
    cleaned = email.strip().casefold()
    return hashlib.sha256(cleaned.encode('utf-8')).hexdigest() if cleaned else ''


def slugify_account_name(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', name.strip().lower()).strip('-')
    return slug[:48] or 'account'


def _default_validator(auth_data: str, timeout: int) -> dict[str, Any]:
    """Validate credentials against Google's Android auth endpoint via GPMC without uploading."""
    from gpmc.api import Api

    api = Api(auth_data=auth_data, timeout=timeout)
    response = api._get_auth_token()
    token = response.get('Auth', '')
    if not token:
        raise ValueError('Authentication response did not contain a bearer token')
    return {'email': parse_qs(auth_data)['Email'][0], 'expiry': response.get('Expiry')}


def _classify_validation_error(error: Exception) -> tuple[str, str]:
    text = str(error).lower()
    status_code = getattr(getattr(error, 'response', None), 'status_code', None)
    if status_code in (401, 403):
        if 'expired' in text or 'token' in text:
            return 'expired', 'Credentials were rejected or have expired (HTTP 401/403).'
        return 'invalid', 'Credentials were rejected by Google authentication (HTTP 401/403).'
    if isinstance(error, ValueError):
        return 'invalid', str(error)
    if 'badauthentication' in text or 'needsbrowser' in text or 'invalid' in text:
        return 'invalid', 'Google rejected the supplied Android authentication credentials.'
    if 'expired' in text:
        return 'expired', 'Google authentication token appears expired; capture fresh AUTH_DATA.'
    return 'error', f'Could not reach Google authentication service ({type(error).__name__}).'


def _write_atomic_secret(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix='.auth_tmp_', dir=str(path.parent))
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
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class AuthManager:
    """Manages one or more Google Photos Android accounts safely without ever exposing raw secrets."""

    def __init__(
        self,
        config: Config,
        client_factory: Callable[..., Any],
        validator: Callable[[str, int], dict[str, Any]] | None = None,
        store: StateStore | None = None,
    ) -> None:
        self.state_dir = config.state_dir
        self.request_timeout = config.request_timeout
        self.log_level = config.log_level
        self.auth_data_file = config.auth_data_file
        self.client_factory = client_factory
        self.validator = validator or _default_validator
        self.store = store
        self._lock = RLock()
        self._override_path = self.state_dir / AUTH_OVERRIDE_FILENAME
        self._accounts_secret_dir = self.state_dir / ACCOUNTS_SECRET_DIR
        self._cache_dir = self.state_dir / GPMC_CACHE_DIR
        self._status_path = self.state_dir / AUTH_STATUS_FILENAME

        # Per-account in-memory runtime credentials & clients (secrets NEVER stored in SQLite).
        self._auth_by_id: dict[int, str] = {}
        self._clients: dict[int, Any] = {}
        self._primary_id: int | None = None

        # Legacy single-account fallback state when initialized without a StateStore.
        self._fallback_auth = config.auth_data
        self._fallback_source = config.auth_source
        self._fallback_status = 'valid' if config.auth_data else 'not_configured'
        self._fallback_hint = self._extract_account_hint(config.auth_data) if config.auth_data else ''
        self._fallback_validated_at: float | None = None
        self._fallback_error: str | None = None
        self._fallback_client: Any = None

        if self.store is not None:
            self._bootstrap_accounts(config)
        elif self._fallback_auth:
            register_redacted_secret(self._fallback_auth)
            self._load_legacy_metadata()
            self._fallback_client = self._build_client(self._fallback_auth, account_id=None)

    @staticmethod
    def _extract_email(auth_data: str) -> str:
        try:
            return parse_qs(auth_data).get('Email', [''])[0].strip()
        except Exception:
            return ''

    @classmethod
    def _extract_account_hint(cls, auth_data: str) -> str:
        email = cls._extract_email(auth_data)
        return mask_email(email) if email else ''

    def _secret_path_for(self, account_id: int, is_primary: bool = False) -> Path:
        if is_primary:
            return self._override_path
        return self._accounts_secret_dir / f'account_{account_id}.secret'

    def _load_legacy_metadata(self) -> dict[str, Any]:
        try:
            if self._status_path.is_file():
                data = json.loads(self._status_path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    self._fallback_validated_at = data.get('last_validated_at')
                    if data.get('status') in ('valid', 'invalid', 'expired', 'error'):
                        self._fallback_status = data['status']
                    self._fallback_error = data.get('last_error')
                    return data
        except (OSError, ValueError):
            pass
        return {}

    def _persist_legacy_metadata(self, account: dict[str, Any]) -> None:
        payload = {
            'status': account['status'],
            'source': account['source'],
            'account_hint': account['account_hint'],
            'last_validated_at': account.get('last_validated_at'),
            'last_error': account.get('last_error'),
            'updated_at': time.time(),
        }
        tmp = self._status_path.with_suffix('.tmp')
        try:
            tmp.write_text(json.dumps(payload), encoding='utf-8')
            tmp.replace(self._status_path)
        except OSError:
            pass

    def _build_client(self, auth_data: str, account_id: int | None) -> Any:
        if not auth_data:
            return None
        client = self.client_factory(
            auth_data=auth_data,
            timeout=self.request_timeout,
            log_level=self.log_level,
        )
        # Keep GPMC local cache DB isolated per account when using real gpmc.Client.
        if account_id is not None and hasattr(client, 'db_path') and not callable(getattr(client, 'db_path', None)):
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                client.db_path = self._cache_dir / f'account_{account_id}.db'
            except (OSError, AttributeError):
                pass
        return client

    def _unique_slug(self, base_name: str) -> str:
        assert self.store is not None
        base = slugify_account_name(base_name)
        candidate = base
        suffix = 2
        while self.store.get_account_by_slug(candidate) is not None:
            candidate = f'{base}-{suffix}'
            suffix += 1
        return candidate

    def _bootstrap_accounts(self, config: Config) -> None:
        assert self.store is not None
        legacy_meta = self._load_legacy_metadata()
        existing = self.store.list_accounts()

        if not existing and config.auth_data:
            email = self._extract_email(config.auth_data)
            initial_status = legacy_meta.get('status') if legacy_meta.get('status') in ('valid', 'invalid', 'expired', 'error') else 'valid'
            primary = self.store.add_account(
                name='Primary Account',
                slug='primary-account',
                account_hint=mask_email(email),
                email_hash=hash_email(email),
                source=config.auth_source,
                status=initial_status,
                is_primary=True,
                last_validated_at=legacy_meta.get('last_validated_at'),
                last_error=legacy_meta.get('last_error'),
            )
            self.store.assign_default_account(int(primary['id']))
            existing = [primary]

        for account in existing:
            aid = int(account['id'])
            is_primary = bool(account['is_primary'])
            if is_primary or self._primary_id is None:
                self._primary_id = aid

            secret_content = ''
            secret_source = account['source']
            account_file = self._accounts_secret_dir / f'account_{aid}.secret'
            if account_file.is_file():
                try:
                    secret_content = account_file.read_text(encoding='utf-8').strip()
                    secret_source = 'account_secret'
                except (OSError, UnicodeError):
                    secret_content = ''
            elif is_primary and self._override_path.is_file():
                try:
                    secret_content = self._override_path.read_text(encoding='utf-8').strip()
                    secret_source = 'managed_override'
                except (OSError, UnicodeError):
                    secret_content = ''
            elif is_primary and config.auth_data:
                secret_content = config.auth_data
                secret_source = config.auth_source

            if secret_content:
                register_redacted_secret(secret_content)
                self._auth_by_id[aid] = secret_content
                email = self._extract_email(secret_content)
                hint = mask_email(email)
                ehash = hash_email(email)
                if hint != account['account_hint'] or secret_source != account['source'] or ehash != account['email_hash']:
                    self.store.update_account(
                        aid, account_hint=hint, email_hash=ehash, source=secret_source
                    )
                if account['status'] not in ('invalid', 'expired'):
                    self._clients[aid] = self._build_client(secret_content, account_id=aid)
            else:
                self.store.update_account(
                    aid,
                    status='not_configured',
                    last_error='Credentials file not found on state storage',
                )

    def _decorate_account(self, account: dict[str, Any]) -> dict[str, Any]:
        aid = int(account['id'])
        is_primary = bool(account['is_primary'])
        configured = aid in self._auth_by_id and bool(self._auth_by_id[aid])
        return {
            **account,
            'configured': configured,
            'has_managed_override': is_primary and self._override_path.is_file(),
            'secret_file_mounted': is_primary and bool(self.auth_data_file and Path(self.auth_data_file).is_file()),
            'secret_file_readonly': is_primary and bool(self.auth_data_file),
        }

    def list_accounts(self) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        with self._lock:
            return [self._decorate_account(acc) for acc in self.store.list_accounts()]

    def get_account(self, account_id: int) -> dict[str, Any] | None:
        if self.store is None:
            return None
        with self._lock:
            acc = self.store.get_account(account_id)
            return self._decorate_account(acc) if acc else None

    def get_primary_account_id(self) -> int | None:
        with self._lock:
            if self._primary_id is not None:
                return self._primary_id
            if self.store is not None:
                primary = self.store.get_primary_account()
                if primary is not None:
                    self._primary_id = int(primary['id'])
                    return self._primary_id
            return None

    def eligible_account_ids(self) -> list[int]:
        if self.store is None:
            return []
        with self._lock:
            ready: list[int] = []
            for acc in self.store.list_accounts():
                aid = int(acc['id'])
                if acc['status'] in ('not_configured', 'invalid', 'expired'):
                    continue
                if aid in self._clients and self._clients[aid] is not None:
                    ready.append(aid)
            return sorted(ready)

    def get_client(self, account_id: int | None = None) -> Any:
        with self._lock:
            if self.store is None:
                if self._fallback_status in ('not_configured', 'invalid', 'expired'):
                    return None
                return self._fallback_client
            target_id = account_id if account_id is not None else self.get_primary_account_id()
            if target_id is None:
                return None
            acc = self.store.get_account(target_id)
            if acc is None or acc['status'] in ('not_configured', 'invalid', 'expired'):
                return None
            return self._clients.get(target_id)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self.store is None:
                return {
                    'status': self._fallback_status,
                    'configured': bool(self._fallback_auth),
                    'account_hint': self._fallback_hint,
                    'source': self._fallback_source,
                    'has_managed_override': self._override_path.is_file(),
                    'secret_file_mounted': bool(self.auth_data_file and Path(self.auth_data_file).is_file()),
                    'secret_file_readonly': bool(self.auth_data_file),
                    'last_validated_at': self._fallback_validated_at,
                    'last_error': self._fallback_error,
                    'accounts_count': 1 if self._fallback_auth else 0,
                    'valid_accounts_count': 1 if self._fallback_status == 'valid' else 0,
                }
            accounts = self.list_accounts()
            valid_count = sum(1 for a in accounts if a['status'] == 'valid')
            primary = next((a for a in accounts if a['is_primary']), accounts[0] if accounts else None)
            if primary is None:
                return {
                    'status': 'not_configured',
                    'configured': False,
                    'account_hint': '',
                    'source': 'none',
                    'has_managed_override': False,
                    'secret_file_mounted': bool(self.auth_data_file and Path(self.auth_data_file).is_file()),
                    'secret_file_readonly': bool(self.auth_data_file),
                    'last_validated_at': None,
                    'last_error': None,
                    'accounts_count': 0,
                    'valid_accounts_count': 0,
                }
            return {
                'account_id': primary['id'],
                'name': primary['name'],
                'status': primary['status'],
                'configured': primary['configured'],
                'account_hint': primary['account_hint'],
                'source': primary['source'],
                'has_managed_override': primary['has_managed_override'],
                'secret_file_mounted': primary['secret_file_mounted'],
                'secret_file_readonly': primary['secret_file_readonly'],
                'last_validated_at': primary['last_validated_at'],
                'last_error': primary['last_error'],
                'accounts_count': len(accounts),
                'valid_accounts_count': valid_count,
            }

    def validate_candidate(self, candidate_auth: str) -> dict[str, Any]:
        """Validate pasted credentials WITHOUT modifying existing working credentials."""
        cleaned = candidate_auth.strip()
        register_redacted_secret(cleaned)
        try:
            fields = validate_auth_data_structure(cleaned)
        except ValueError as error:
            return {
                'valid': False,
                'status': 'invalid',
                'account_hint': '',
                'message': str(error),
            }
        email = fields['Email'][0].strip()
        hint = mask_email(email)
        try:
            self.validator(cleaned, self.request_timeout)
        except Exception as error:
            status_code, message = _classify_validation_error(error)
            return {
                'valid': False,
                'status': status_code,
                'account_hint': hint,
                'message': message,
            }
        return {
            'valid': True,
            'status': 'valid',
            'account_hint': hint,
            'message': 'Credentials validated successfully with Google Photos authentication.',
        }

    def add_account(self, name: str, candidate_auth: str) -> dict[str, Any]:
        """Validate credentials and add a new connected Google account with isolated 0600 secret storage."""
        if self.store is None:
            raise RuntimeError('Multi-account management requires a persistent StateStore')
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError('Account name cannot be empty')
        cleaned_auth = candidate_auth.strip()
        register_redacted_secret(cleaned_auth)

        validation = self.validate_candidate(cleaned_auth)
        if not validation['valid']:
            return {**validation, 'saved': False}

        email = self._extract_email(cleaned_auth)
        ehash = hash_email(email)
        with self._lock:
            existing_same = self.store.get_account_by_email_hash(ehash)
            if existing_same is not None:
                return {
                    'valid': False,
                    'saved': False,
                    'status': 'invalid',
                    'account_hint': validation['account_hint'],
                    'message': f'This Google account is already connected as "{existing_same["name"]}". Rotate its credentials instead.',
                }
            is_first = len(self.store.list_accounts()) == 0
            slug = self._unique_slug(cleaned_name)
            now = time.time()
            account = self.store.add_account(
                name=cleaned_name,
                slug=slug,
                account_hint=validation['account_hint'],
                email_hash=ehash,
                source='managed_override' if is_first else 'account_secret',
                status='valid',
                is_primary=is_first,
                last_validated_at=now,
            )
            aid = int(account['id'])
            secret_path = self._secret_path_for(aid, is_primary=is_first)
            _write_atomic_secret(secret_path, cleaned_auth)
            self._auth_by_id[aid] = cleaned_auth
            self._clients[aid] = self._build_client(cleaned_auth, account_id=aid)
            if is_first:
                self._primary_id = aid
                self.store.assign_default_account(aid)
                self._persist_legacy_metadata(account)
            decorated = self._decorate_account(self.store.get_account(aid) or account)
            logger.info('Connected Google account id=%s name=%s account=%s', aid, cleaned_name, decorated['account_hint'])
            return {
                **validation,
                'saved': True,
                'account': decorated,
                'auth_status': self.status(),
            }

    def rename_account(self, account_id: int, name: str) -> dict[str, Any]:
        if self.store is None:
            raise RuntimeError('Multi-account management requires a persistent StateStore')
        cleaned = name.strip()
        if not cleaned:
            raise ValueError('Account name cannot be empty')
        with self._lock:
            existing = self.store.get_account(account_id)
            if existing is None:
                raise KeyError('Account not found')
            updated = self.store.update_account(account_id, name=cleaned)
            assert updated is not None
            return self._decorate_account(updated)

    def replace_account_credentials(self, account_id: int, candidate_auth: str) -> dict[str, Any]:
        """Validate replacement credentials first; only atomically overwrite on success."""
        if self.store is None:
            raise RuntimeError('Multi-account management requires a persistent StateStore')
        cleaned = candidate_auth.strip()
        register_redacted_secret(cleaned)
        with self._lock:
            existing = self.store.get_account(account_id)
            if existing is None:
                raise KeyError('Account not found')
            previous_status = existing['status']
            self.store.update_account(account_id, status='validating')

        validation = self.validate_candidate(cleaned)
        if not validation['valid']:
            with self._lock:
                # Preserve existing working credentials untouched when validation fails!
                self.store.update_account(account_id, status=previous_status)
            return {**validation, 'saved': False}

        email = self._extract_email(cleaned)
        ehash = hash_email(email)
        with self._lock:
            conflict = self.store.get_account_by_email_hash(ehash)
            if conflict is not None and int(conflict['id']) != account_id:
                self.store.update_account(account_id, status=previous_status)
                return {
                    'valid': False,
                    'saved': False,
                    'status': 'invalid',
                    'account_hint': validation['account_hint'],
                    'message': f'Those credentials belong to another connected account ("{conflict["name"]}").',
                }
            is_primary = bool(existing['is_primary'])
            secret_path = self._secret_path_for(account_id, is_primary=is_primary)
            _write_atomic_secret(secret_path, cleaned)
            source = 'managed_override' if is_primary else 'account_secret'
            self._auth_by_id[account_id] = cleaned
            self._clients[account_id] = self._build_client(cleaned, account_id=account_id)
            updated = self.store.update_account(
                account_id,
                account_hint=validation['account_hint'],
                email_hash=ehash,
                source=source,
                status='valid',
                last_validated_at=time.time(),
                clear_error=True,
            )
            assert updated is not None
            if is_primary:
                self._persist_legacy_metadata(updated)
            decorated = self._decorate_account(updated)
            logger.info('Rotated credentials for Google account id=%s account=%s source=%s', account_id, decorated['account_hint'], source)
            return {
                **validation,
                'saved': True,
                'account': decorated,
                'auth_status': self.status(),
            }

    def save_validated_credentials(self, candidate_auth: str) -> dict[str, Any]:
        """Legacy single-account endpoint helper: updates primary account or creates one."""
        if self.store is not None:
            primary_id = self.get_primary_account_id()
            if primary_id is None:
                return self.add_account('Primary Account', candidate_auth)
            return self.replace_account_credentials(primary_id, candidate_auth)

        cleaned = candidate_auth.strip()
        register_redacted_secret(cleaned)
        validation = self.validate_candidate(cleaned)
        if not validation['valid']:
            return {**validation, 'saved': False}
        with self._lock:
            _write_atomic_secret(self._override_path, cleaned)
            self._fallback_auth = cleaned
            self._fallback_source = 'managed_override'
            self._fallback_hint = validation['account_hint']
            self._fallback_status = 'valid'
            self._fallback_validated_at = time.time()
            self._fallback_error = None
            self._fallback_client = self._build_client(cleaned, account_id=None)
            return {**validation, 'saved': True, 'auth_status': self.status()}

    def revalidate_account(self, account_id: int) -> dict[str, Any]:
        if self.store is None:
            raise RuntimeError('Multi-account management requires a persistent StateStore')
        with self._lock:
            existing = self.store.get_account(account_id)
            if existing is None:
                raise KeyError('Account not found')
            current = self._auth_by_id.get(account_id, '')
            if not current:
                updated = self.store.update_account(
                    account_id,
                    status='not_configured',
                    last_error='No authentication credentials configured for this account.',
                )
                return {
                    'valid': False,
                    'status': 'not_configured',
                    'message': 'No authentication credentials configured for this account.',
                    'account': self._decorate_account(updated or existing),
                    'auth_status': self.status(),
                }
            self.store.update_account(account_id, status='validating')

        validation = self.validate_candidate(current)
        with self._lock:
            if validation['valid']:
                updated = self.store.update_account(
                    account_id,
                    status='valid',
                    last_validated_at=time.time(),
                    clear_error=True,
                )
                if account_id not in self._clients or self._clients[account_id] is None:
                    self._clients[account_id] = self._build_client(current, account_id=account_id)
            else:
                updated = self.store.update_account(
                    account_id,
                    status=validation['status'],
                    last_error=validation['message'],
                )
            assert updated is not None
            if updated['is_primary']:
                self._persist_legacy_metadata(updated)
            return {
                **validation,
                'account': self._decorate_account(updated),
                'auth_status': self.status(),
            }

    def revalidate_current(self) -> dict[str, Any]:
        if self.store is not None:
            primary_id = self.get_primary_account_id()
            if primary_id is None:
                return {
                    'valid': False,
                    'status': 'not_configured',
                    'message': 'No authentication credentials configured.',
                    'auth_status': self.status(),
                }
            return self.revalidate_account(primary_id)
        if not self._fallback_auth:
            return {'valid': False, 'status': 'not_configured', 'message': 'No credentials configured.', 'auth_status': self.status()}
        validation = self.validate_candidate(self._fallback_auth)
        with self._lock:
            self._fallback_status = validation['status']
            self._fallback_error = None if validation['valid'] else validation['message']
            if validation['valid']:
                self._fallback_validated_at = time.time()
            return {**validation, 'auth_status': self.status()}

    def remove_account(self, account_id: int) -> dict[str, Any]:
        if self.store is None:
            raise RuntimeError('Multi-account management requires a persistent StateStore')
        with self._lock:
            existing = self.store.get_account(account_id)
            if existing is None:
                raise KeyError('Account not found')
            is_primary = bool(existing['is_primary'])
            # Delete only managed secret files; never touch read-only /run/secrets/gphotos_auth.
            account_file = self._accounts_secret_dir / f'account_{account_id}.secret'
            try:
                account_file.unlink(missing_ok=True)
            except OSError:
                pass
            if is_primary and self._override_path.is_file():
                try:
                    self._override_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._auth_by_id.pop(account_id, None)
            self._clients.pop(account_id, None)
            self.store.remove_account(account_id)
            if self._primary_id == account_id:
                remaining = self.store.get_primary_account()
                self._primary_id = int(remaining['id']) if remaining else None
            logger.info('Removed Google account id=%s name=%s', account_id, existing['name'])
            return {'removed': True, 'account_id': account_id, 'name': existing['name']}

    def report_runtime_auth_error(self, error: Exception, account_id: int | None = None) -> int | None:
        status_code, message = _classify_validation_error(error)
        if status_code not in ('invalid', 'expired'):
            return None
        with self._lock:
            if self.store is None:
                self._fallback_status = status_code
                self._fallback_error = message
                return None
            target_id = account_id if account_id is not None else self.get_primary_account_id()
            if target_id is None:
                return None
            updated = self.store.update_account(target_id, status=status_code, last_error=message)
            if updated and updated['is_primary']:
                self._persist_legacy_metadata(updated)
            return target_id
