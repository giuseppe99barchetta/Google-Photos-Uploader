"""Dashboard authentication, session management, CSRF validation, and rate limiting."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any

DASHBOARD_AUTH_FILENAME = 'dashboard_auth.json'
SESSION_COOKIE_NAME = 'gphotos_session'
CSRF_HEADER_NAME = 'x-csrf-token'
PBKDF2_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 86_400
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f'Password must be at least {MIN_PASSWORD_LENGTH} characters')
    salt_bytes = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt_bytes, PBKDF2_ITERATIONS)
    return base64.b64encode(salt_bytes).decode('ascii'), base64.b64encode(digest).decode('ascii')


def verify_password(password: str, salt_b64: str, digest_b64: str) -> bool:
    try:
        salt_bytes = base64.b64decode(salt_b64.encode('ascii'))
        expected = base64.b64decode(digest_b64.encode('ascii'))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt_bytes, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._lock = Lock()
        self._buckets: dict[str, deque[float]] = {}

    def check_and_record(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                retry_after = max(1, int(bucket[0] + self.window_seconds - now) + 1)
                return False, retry_after
            bucket.append(now)
            return True, 0


class DashboardAuthManager:
    """Manages dashboard login credentials, signed sessions, and CSRF tokens."""

    def __init__(
        self,
        state_dir: Path,
        username: str = 'admin',
        initial_password: str = '',
        session_secret: str = '',
        secure_cookies: bool = False,
    ) -> None:
        self.state_dir = state_dir
        self.secure_cookies = secure_cookies
        self._auth_path = state_dir / DASHBOARD_AUTH_FILENAME
        self._lock = Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._username = username.strip() or 'admin'
        self._salt_b64 = ''
        self._digest_b64 = ''
        self._session_secret = session_secret.strip()
        self.login_limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=300)
        self.secret_limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=300)
        self._load_or_initialize(initial_password)

    def _load_or_initialize(self, initial_password: str) -> None:
        loaded: dict[str, Any] = {}
        if self._auth_path.is_file():
            try:
                loaded = json.loads(self._auth_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                loaded = {}

        if not self._session_secret:
            self._session_secret = str(loaded.get('session_secret') or secrets.token_hex(32))
        if loaded.get('username') and not initial_password:
            self._username = str(loaded['username'])
        self._salt_b64 = str(loaded.get('salt') or '')
        self._digest_b64 = str(loaded.get('digest') or '')

        dirty = not self._auth_path.is_file()
        if initial_password:
            if not self._salt_b64 or not verify_password(initial_password, self._salt_b64, self._digest_b64):
                self._salt_b64, self._digest_b64 = hash_password(initial_password)
                dirty = True
        if dirty:
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            'username': self._username,
            'salt': self._salt_b64,
            'digest': self._digest_b64,
            'session_secret': self._session_secret,
            'updated_at': time.time(),
        }
        fd, tmp_name = tempfile.mkstemp(prefix='.dash_auth_', dir=str(self.state_dir))
        tmp_path = Path(tmp_name)
        try:
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(json.dumps(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._auth_path)
            try:
                os.chmod(self._auth_path, 0o600)
            except OSError:
                pass
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    @property
    def setup_required(self) -> bool:
        with self._lock:
            return not (self._salt_b64 and self._digest_b64)

    def complete_setup(self, username: str, password: str) -> dict[str, Any]:
        cleaned_user = username.strip()
        if not cleaned_user:
            raise ValueError('Username cannot be empty')
        with self._lock:
            if self._salt_b64 and self._digest_b64:
                raise PermissionError('Initial dashboard setup is already complete')
            self._username = cleaned_user
            self._salt_b64, self._digest_b64 = hash_password(password)
            self._save_unlocked()
            return self._create_session_unlocked(cleaned_user)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with self._lock:
            if not (self._salt_b64 and self._digest_b64):
                return None
            user_match = hmac.compare_digest(username.strip(), self._username)
            pass_match = verify_password(password, self._salt_b64, self._digest_b64)
            if not (user_match and pass_match):
                return None
            return self._create_session_unlocked(self._username)

    def change_password(self, current_password: str, new_password: str) -> bool:
        with self._lock:
            if not verify_password(current_password, self._salt_b64, self._digest_b64):
                return False
            self._salt_b64, self._digest_b64 = hash_password(new_password)
            self._save_unlocked()
            return True

    def _sign_sid(self, sid: str) -> str:
        sig = hmac.new(
            self._session_secret.encode('utf-8'), sid.encode('utf-8'), hashlib.sha256
        ).hexdigest()
        return f'{sid}.{sig}'

    def _verify_cookie(self, cookie_value: str | None) -> str | None:
        if not cookie_value or '.' not in cookie_value:
            return None
        sid, provided_sig = cookie_value.rsplit('.', 1)
        expected_sig = hmac.new(
            self._session_secret.encode('utf-8'), sid.encode('utf-8'), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(provided_sig, expected_sig):
            return None
        return sid

    def _create_session_unlocked(self, username: str) -> dict[str, Any]:
        self._prune_expired_unlocked()
        sid = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = time.time() + SESSION_TTL_SECONDS
        self._sessions[sid] = {
            'username': username,
            'csrf_token': csrf_token,
            'expires_at': expires_at,
        }
        return {
            'cookie_value': self._sign_sid(sid),
            'username': username,
            'csrf_token': csrf_token,
            'expires_at': expires_at,
        }

    def _prune_expired_unlocked(self) -> None:
        now = time.time()
        expired = [sid for sid, data in self._sessions.items() if data['expires_at'] <= now]
        for sid in expired:
            self._sessions.pop(sid, None)

    def get_session(self, cookie_value: str | None) -> dict[str, Any] | None:
        sid = self._verify_cookie(cookie_value)
        if sid is None:
            return None
        with self._lock:
            self._prune_expired_unlocked()
            session = self._sessions.get(sid)
            return dict(session) if session is not None else None

    def validate_csrf(self, cookie_value: str | None, csrf_header: str | None) -> bool:
        session = self.get_session(cookie_value)
        if session is None or not csrf_header:
            return False
        return hmac.compare_digest(session['csrf_token'], csrf_header.strip())

    def logout(self, cookie_value: str | None) -> None:
        sid = self._verify_cookie(cookie_value)
        if sid is None:
            return
        with self._lock:
            self._sessions.pop(sid, None)
