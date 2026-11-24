"""Durable, single-worker upload queue and sync manager state. Keep this database on local storage."""

import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import RLock
from typing import Any

CURRENT_SCHEMA_VERSION = 3
MAX_ACTIVITY_LOG_ROWS = 10000
DEFAULT_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.heic', '.mp4', '.mov', '.mkv')


class StateStore:
    def __init__(self, path: str | Path, scope: str | None = None) -> None:
        self.db_path = str(path)
        self.lock = RLock()
        existed_before_open = self.db_path != ':memory:' and Path(self.db_path).is_file() and Path(self.db_path).stat().st_size > 0
        self.connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA busy_timeout=5000')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS jobs (
            path TEXT PRIMARY KEY, signature TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', media_key TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT
        )''')
        self.connection.execute('CREATE INDEX IF NOT EXISTS due_jobs ON jobs(status, next_attempt)')
        self.connection.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
        self._run_migrations(backup_existing=existed_before_open)
        migrated = self.connection.execute(
            "SELECT value FROM metadata WHERE key='signature_fields'").fetchone()
        if migrated is None:
            with self.connection:
                # Signatures lost ctime; json.dumps keeps the string form that queries compare.
                for row in self.connection.execute('SELECT path, signature FROM jobs').fetchall():
                    fields = json.loads(row['signature'])
                    if len(fields) == 5:
                        self.connection.execute('UPDATE jobs SET signature=? WHERE path=?',
                                                (json.dumps(fields[:4]), row['path']))
                self.connection.execute("INSERT OR REPLACE INTO metadata VALUES ('signature_fields', '4')")
        if scope is not None:
            previous = self.connection.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()
            with self.connection:
                if previous is None or previous['value'] != scope:
                    self.connection.execute('DELETE FROM jobs')
                    self.connection.execute("INSERT OR REPLACE INTO metadata VALUES ('scope', ?)", (scope,))
        self.connection.commit()

    def _get_metadata(self, key: str) -> str | None:
        row = self.connection.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return str(row['value']) if row is not None and row['value'] is not None else None

    def _set_metadata(self, key: str, value: str) -> None:
        self.connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, value))

    def _backup_database(self, from_version: int, to_version: int) -> Path | None:
        if self.db_path == ':memory:':
            return None
        source = Path(self.db_path)
        if not source.is_file():
            return None
        timestamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        backup_path = source.with_name(f'{source.stem}.backup-v{from_version}-to-v{to_version}-{timestamp}{source.suffix}')
        backup_conn = sqlite3.connect(str(backup_path))
        try:
            self.connection.backup(backup_conn)
        finally:
            backup_conn.close()
        return backup_path

    def _run_migrations(self, backup_existing: bool = False) -> None:
        raw_version = self._get_metadata('schema_version')
        current_version = int(raw_version) if raw_version and raw_version.isdigit() else 1
        if current_version >= CURRENT_SCHEMA_VERSION:
            return
        if backup_existing:
            self._backup_database(current_version, CURRENT_SCHEMA_VERSION)
        if current_version < 2:
            self._migrate_v1_to_v2()
        if current_version < 3:
            self._migrate_v2_to_v3()

    def _migrate_v1_to_v2(self) -> None:
        existing_columns = {
            row['name'] for row in self.connection.execute('PRAGMA table_info(jobs)').fetchall()
        }
        new_columns = [
            ('folder_id', 'INTEGER'),
            ('file_size', 'INTEGER NOT NULL DEFAULT 0'),
            ('file_ext', "TEXT NOT NULL DEFAULT ''"),
            ('created_at', 'REAL NOT NULL DEFAULT 0'),
            ('updated_at', 'REAL NOT NULL DEFAULT 0'),
            ('last_attempt_at', 'REAL'),
            ('completed_at', 'REAL'),
            ('upload_duration', 'REAL'),
            ('result_status', 'TEXT'),
        ]
        with self.connection:
            for col_name, col_def in new_columns:
                if col_name not in existing_columns:
                    self.connection.execute(f'ALTER TABLE jobs ADD COLUMN {col_name} {col_def}')
            self.connection.execute('''CREATE TABLE IF NOT EXISTS monitored_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                paused INTEGER NOT NULL DEFAULT 0,
                recursive INTEGER NOT NULL DEFAULT 1,
                extensions TEXT NOT NULL,
                exclude_dirs TEXT NOT NULL DEFAULT '[]',
                exclude_patterns TEXT NOT NULL DEFAULT '[]',
                min_file_size INTEGER NOT NULL DEFAULT 1,
                max_file_size INTEGER,
                last_scan_at REAL,
                last_detected_at REAL,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                health TEXT NOT NULL DEFAULT 'unknown',
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )''')
            self.connection.execute('''CREATE TABLE IF NOT EXISTS upload_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                folder_id INTEGER,
                folder_name TEXT,
                file_size INTEGER NOT NULL DEFAULT 0,
                file_ext TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL,
                media_key TEXT,
                duration REAL,
                error TEXT,
                created_at REAL NOT NULL
            )''')
            self.connection.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                category TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                details TEXT,
                created_at REAL NOT NULL
            )''')
            self.connection.execute('''CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_key TEXT NOT NULL,
                severity TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                read INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )''')
            self.connection.execute('''CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            self.connection.execute('CREATE INDEX IF NOT EXISTS jobs_folder_status ON jobs(folder_id, status)')
            self.connection.execute('CREATE INDEX IF NOT EXISTS history_created ON upload_history(created_at DESC)')
            self.connection.execute('CREATE INDEX IF NOT EXISTS history_outcome ON upload_history(outcome)')
            self.connection.execute('CREATE INDEX IF NOT EXISTS activity_created ON activity_logs(created_at DESC)')
            self.connection.execute('CREATE INDEX IF NOT EXISTS notifications_issue ON notifications(issue_key, resolved)')
            self._set_metadata('schema_version', '2')

    def _migrate_v2_to_v3(self) -> None:
        with self.connection:
            self.connection.execute('''CREATE TABLE IF NOT EXISTS google_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                account_hint TEXT NOT NULL DEFAULT '',
                email_hash TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'account_secret',
                status TEXT NOT NULL DEFAULT 'not_configured',
                last_validated_at REAL,
                last_error TEXT,
                is_primary INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )''')
            for table_name, columns in (
                ('monitored_folders', [('account_id', 'INTEGER')]),
                ('jobs', [('account_id', 'INTEGER')]),
                ('upload_history', [('account_id', 'INTEGER'), ('account_name', 'TEXT')]),
            ):
                existing_cols = {
                    row['name']
                    for row in self.connection.execute(f'PRAGMA table_info({table_name})').fetchall()
                }
                for col_name, col_def in columns:
                    if col_name not in existing_cols:
                        self.connection.execute(
                            f'ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}'
                        )
            self.connection.execute(
                'CREATE INDEX IF NOT EXISTS jobs_account_status ON jobs(account_id, status, next_attempt)'
            )
            self.connection.execute(
                'CREATE INDEX IF NOT EXISTS folders_account ON monitored_folders(account_id)'
            )
            self.connection.execute(
                'CREATE INDEX IF NOT EXISTS history_account ON upload_history(account_id, created_at DESC)'
            )
            self._set_metadata('schema_version', '3')

    # --- Google Accounts (metadata only; secrets never stored in SQLite) ---

    @staticmethod
    def _decode_account(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data['is_primary'] = bool(data['is_primary'])
        return data

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                '''SELECT a.*,
                          (SELECT COUNT(*) FROM monitored_folders f WHERE f.account_id = a.id) AS folder_count,
                          (SELECT COUNT(*) FROM jobs j WHERE j.account_id = a.id AND j.status='pending') AS pending_count,
                          (SELECT COUNT(*) FROM jobs j WHERE j.account_id = a.id AND j.status='done') AS uploaded_count,
                          (SELECT COUNT(*) FROM jobs j WHERE j.account_id = a.id AND j.status='failed') AS failed_count
                   FROM google_accounts a
                   ORDER BY a.is_primary DESC, a.id ASC'''
            ).fetchall()
            return [self._decode_account(row) for row in rows]

    def get_account(self, account_id: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                '''SELECT a.*,
                          (SELECT COUNT(*) FROM monitored_folders f WHERE f.account_id = a.id) AS folder_count,
                          (SELECT COUNT(*) FROM jobs j WHERE j.account_id = a.id AND j.status='pending') AS pending_count,
                          (SELECT COUNT(*) FROM jobs j WHERE j.account_id = a.id AND j.status='done') AS uploaded_count,
                          (SELECT COUNT(*) FROM jobs j WHERE j.account_id = a.id AND j.status='failed') AS failed_count
                   FROM google_accounts a
                   WHERE a.id=?''',
                (account_id,),
            ).fetchone()
            return self._decode_account(row) if row else None

    def get_primary_account(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                'SELECT id FROM google_accounts ORDER BY is_primary DESC, id ASC LIMIT 1'
            ).fetchone()
            return self.get_account(int(row['id'])) if row else None

    def get_account_by_email_hash(self, email_hash: str) -> dict[str, Any] | None:
        if not email_hash:
            return None
        with self.lock:
            row = self.connection.execute(
                'SELECT id FROM google_accounts WHERE email_hash=?', (email_hash,)
            ).fetchone()
            return self.get_account(int(row['id'])) if row else None

    def get_account_by_slug(self, slug: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                'SELECT id FROM google_accounts WHERE slug=?', (slug,)
            ).fetchone()
            return self.get_account(int(row['id'])) if row else None

    def add_account(
        self,
        *,
        name: str,
        slug: str,
        account_hint: str = '',
        email_hash: str = '',
        source: str = 'account_secret',
        status: str = 'not_configured',
        is_primary: bool = False,
        last_validated_at: float | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self.lock, self.connection:
            cursor = self.connection.execute(
                '''INSERT INTO google_accounts(
                       name, slug, account_hint, email_hash, source, status,
                       last_validated_at, last_error, is_primary, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    name.strip(),
                    slug.strip(),
                    account_hint,
                    email_hash,
                    source,
                    status,
                    last_validated_at,
                    last_error,
                    int(bool(is_primary)),
                    now,
                    now,
                ),
            )
            account_id = int(cursor.lastrowid)
        account = self.get_account(account_id)
        assert account is not None
        return account

    def update_account(
        self,
        account_id: int,
        *,
        name: str | None = None,
        account_hint: str | None = None,
        email_hash: str | None = None,
        source: str | None = None,
        status: str | None = None,
        last_validated_at: float | None = None,
        last_error: str | None = None,
        clear_error: bool = False,
    ) -> dict[str, Any] | None:
        assignments: list[str] = []
        values: list[Any] = []
        if name is not None:
            assignments.append('name=?')
            values.append(name.strip())
        if account_hint is not None:
            assignments.append('account_hint=?')
            values.append(account_hint)
        if email_hash is not None:
            assignments.append('email_hash=?')
            values.append(email_hash)
        if source is not None:
            assignments.append('source=?')
            values.append(source)
        if status is not None:
            assignments.append('status=?')
            values.append(status)
        if last_validated_at is not None:
            assignments.append('last_validated_at=?')
            values.append(last_validated_at)
        if clear_error:
            assignments.append('last_error=NULL')
        elif last_error is not None:
            assignments.append('last_error=?')
            values.append(last_error)
        if not assignments:
            return self.get_account(account_id)
        assignments.append('updated_at=?')
        values.append(time.time())
        values.append(account_id)
        with self.lock, self.connection:
            self.connection.execute(
                f"UPDATE google_accounts SET {', '.join(assignments)} WHERE id=?",
                values,
            )
        return self.get_account(account_id)

    def remove_account(self, account_id: int) -> bool:
        now = time.time()
        with self.lock, self.connection:
            # Pause and unbind folders so they never silently route to another account.
            self.connection.execute(
                "UPDATE monitored_folders SET account_id=NULL, paused=1, health='paused', updated_at=? WHERE account_id=?",
                (now, account_id),
            )
            self.connection.execute(
                'UPDATE jobs SET account_id=NULL, updated_at=? WHERE account_id=?',
                (now, account_id),
            )
            cursor = self.connection.execute('DELETE FROM google_accounts WHERE id=?', (account_id,))
            return cursor.rowcount > 0

    def assign_default_account(self, account_id: int) -> None:
        """Backfill legacy v1/v2 records that predate multi-account support to the primary account."""
        now = time.time()
        with self.lock, self.connection:
            self.connection.execute(
                'UPDATE monitored_folders SET account_id=?, updated_at=? WHERE account_id IS NULL',
                (account_id, now),
            )
            self.connection.execute(
                'UPDATE jobs SET account_id=? WHERE account_id IS NULL',
                (account_id,),
            )
            account = self.connection.execute(
                'SELECT name FROM google_accounts WHERE id=?', (account_id,)
            ).fetchone()
            acc_name = account['name'] if account else None
            self.connection.execute(
                'UPDATE upload_history SET account_id=?, account_name=COALESCE(account_name, ?) WHERE account_id IS NULL',
                (account_id, acc_name),
            )

    def schema_version(self) -> int:
        with self.lock:
            raw = self._get_metadata('schema_version')
            return int(raw) if raw and raw.isdigit() else 1

    def db_stats(self) -> dict[str, Any]:
        with self.lock:
            self.connection.execute('SELECT 1').fetchone()
            size_bytes = 0
            if self.db_path != ':memory:' and Path(self.db_path).is_file():
                size_bytes = Path(self.db_path).stat().st_size
            return {
                'ok': True,
                'schema_version': self.schema_version(),
                'size_bytes': size_bytes,
            }

    # --- Queue / Jobs ---

    def enqueue(
        self,
        path: str | Path,
        signature: Sequence[int],
        folder_id: int | None = None,
        file_size: int | None = None,
        auto_retry_failed: bool = True,
        account_id: int | None = None,
    ) -> bool:
        serialized = json.dumps(signature)
        now = time.time()
        resolved_size = int(file_size) if file_size is not None else (int(signature[2]) if len(signature) >= 3 else 0)
        file_ext = Path(path).suffix.lower()
        with self.lock, self.connection:
            existing = self.connection.execute('SELECT * FROM jobs WHERE path=?', (str(path),)).fetchone()
            if existing and existing['signature'] == serialized:
                # If the folder was reassigned to a different Google account, upload fresh to the new account.
                if (account_id is not None and existing['account_id'] is not None
                        and int(existing['account_id']) != int(account_id)):
                    self.connection.execute(
                        '''UPDATE jobs SET account_id=?, folder_id=COALESCE(?, folder_id),
                           status='pending', media_key=NULL, attempts=0, next_attempt=0,
                           last_error=NULL, result_status=NULL, updated_at=?
                           WHERE path=?''',
                        (account_id, folder_id, now, str(path)),
                    )
                    return True
                if existing['status'] == 'cancelled':
                    return False
                if ((folder_id is not None and existing['folder_id'] is None)
                        or (account_id is not None and existing['account_id'] is None)):
                    self.connection.execute(
                        '''UPDATE jobs SET folder_id=COALESCE(folder_id, ?),
                           account_id=COALESCE(account_id, ?), file_size=?, file_ext=?, updated_at=?
                           WHERE path=?''',
                        (folder_id, account_id, resolved_size, file_ext, now, str(path)),
                    )
                if existing['status'] == 'failed' and auto_retry_failed and existing['next_attempt'] <= now:
                    self.connection.execute(
                        "UPDATE jobs SET status='pending', attempts=0, updated_at=? WHERE path=?",
                        (now, str(path)),
                    )
                    return True
                return existing['status'] == 'pending'
            self.connection.execute(
                '''INSERT INTO jobs(path, signature, folder_id, account_id, file_size, file_ext, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       signature=excluded.signature,
                       status='pending',
                       media_key=NULL,
                       attempts=0,
                       next_attempt=0,
                       last_error=NULL,
                       result_status=NULL,
                       folder_id=COALESCE(excluded.folder_id, jobs.folder_id),
                       account_id=COALESCE(excluded.account_id, jobs.account_id),
                       file_size=excluded.file_size,
                       file_ext=excluded.file_ext,
                       created_at=CASE WHEN jobs.created_at = 0 THEN excluded.created_at ELSE jobs.created_at END,
                       updated_at=excluded.updated_at''',
                (str(path), serialized, folder_id, account_id, resolved_size, file_ext, now, now),
            )
            return True

    def next_jobs(
        self,
        limit: int = 1,
        exclude_paused_folders: bool = True,
        account_id: int | None = None,
        eligible_account_ids: Sequence[int] | None = None,
        last_account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError('limit must be at least 1')
        now = time.time()
        folder_clause = (
            'AND (j.folder_id IS NULL OR f.id IS NULL OR (f.enabled=1 AND f.paused=0))'
            if exclude_paused_folders
            else ''
        )
        with self.lock:
            if eligible_account_ids is not None:
                # First handle any due cleanup jobs (already confirmed remotely; no account client needed).
                cleanup_rows = self.connection.execute(
                    f'''SELECT j.* FROM jobs j
                        LEFT JOIN monitored_folders f ON j.folder_id = f.id
                        WHERE j.status='pending' AND j.media_key IS NOT NULL AND j.next_attempt<=?
                          {folder_clause}
                        ORDER BY j.next_attempt, j.rowid LIMIT ?''',
                    (now, limit),
                ).fetchall()
                if cleanup_rows:
                    return [dict(row) for row in cleanup_rows]

                eligible = sorted({int(aid) for aid in eligible_account_ids})
                if not eligible:
                    return []
                placeholders = ','.join('?' for _ in eligible)
                due_acc_rows = self.connection.execute(
                    f'''SELECT DISTINCT j.account_id FROM jobs j
                        LEFT JOIN monitored_folders f ON j.folder_id = f.id
                        WHERE j.status='pending' AND j.media_key IS NULL AND j.next_attempt<=?
                          AND j.account_id IN ({placeholders})
                          {folder_clause}
                        ORDER BY j.account_id ASC''',
                    (now, *eligible),
                ).fetchall()
                due_accounts = [int(row['account_id']) for row in due_acc_rows]
                if not due_accounts:
                    return []
                if last_account_id is not None:
                    after = [aid for aid in due_accounts if aid > last_account_id]
                    selected_account = after[0] if after else due_accounts[0]
                else:
                    selected_account = due_accounts[0]
                rows = self.connection.execute(
                    f'''SELECT j.* FROM jobs j
                        LEFT JOIN monitored_folders f ON j.folder_id = f.id
                        WHERE j.status='pending' AND j.media_key IS NULL AND j.next_attempt<=?
                          AND j.account_id=?
                          {folder_clause}
                        ORDER BY j.next_attempt, j.rowid LIMIT ?''',
                    (now, selected_account, limit),
                ).fetchall()
                return [dict(row) for row in rows]

            if account_id is not None:
                rows = self.connection.execute(
                    f'''SELECT j.* FROM jobs j
                        LEFT JOIN monitored_folders f ON j.folder_id = f.id
                        WHERE j.status='pending' AND j.next_attempt<=? AND j.account_id=?
                          {folder_clause}
                        ORDER BY j.next_attempt, j.rowid LIMIT ?''',
                    (now, account_id, limit),
                ).fetchall()
                return [dict(row) for row in rows]

            if exclude_paused_folders:
                rows = self.connection.execute(
                    '''SELECT j.* FROM jobs j
                       LEFT JOIN monitored_folders f ON j.folder_id = f.id
                       WHERE j.status='pending' AND j.next_attempt<=?
                         AND (j.folder_id IS NULL OR f.id IS NULL OR (f.enabled=1 AND f.paused=0))
                       ORDER BY j.next_attempt, j.rowid LIMIT ?''',
                    (now, limit),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    '''SELECT * FROM jobs
                       WHERE status='pending' AND next_attempt<=?
                       ORDER BY next_attempt, rowid LIMIT ?''',
                    (now, limit),
                ).fetchall()
            return [dict(row) for row in rows]

    def next_job(self) -> dict[str, Any] | None:
        jobs = self.next_jobs(1)
        return jobs[0] if jobs else None

    def mark_uploaded(self, job: dict[str, Any], signature: Sequence[int], media_key: str) -> bool:
        serialized = json.dumps(signature)
        now = time.time()
        file_size = int(signature[2]) if len(signature) >= 3 else int(job.get('file_size') or 0)
        with self.lock, self.connection:
            cursor = self.connection.execute(
                '''UPDATE jobs SET signature=?, media_key=?, file_size=?,
                   attempts=0, next_attempt=0, last_error=NULL, last_attempt_at=?, updated_at=?
                   WHERE path=? AND signature IN (?, ?)''',
                (serialized, media_key, file_size, now, now, job['path'], job['signature'], serialized),
            )
            return cursor.rowcount > 0

    def complete(
        self,
        path: str | Path,
        signature: str,
        duration: float | None = None,
        result_status: str = 'uploaded',
    ) -> None:
        now = time.time()
        with self.lock, self.connection:
            self.connection.execute(
                '''UPDATE jobs SET status='done', last_error=NULL, completed_at=?,
                   upload_duration=?, result_status=?, updated_at=?
                   WHERE path=? AND signature=?''',
                (now, duration, result_status, now, str(path), signature),
            )

    def fail(
        self,
        job: dict[str, Any],
        error: Exception,
        max_attempts: int,
        retry_base: float,
        retry_max: float,
    ) -> tuple[int, float, bool]:
        attempts = job['attempts'] + 1
        delay = min(retry_max, retry_base * 2 ** min(attempts - 1, 20))
        exhausted = attempts >= max_attempts
        now = time.time()
        with self.lock, self.connection:
            self.connection.execute(
                '''UPDATE jobs SET attempts=?, next_attempt=?,
                   status=?, last_error=?, last_attempt_at=?, updated_at=?, result_status=?
                   WHERE path=? AND signature=?''',
                (
                    attempts,
                    now + (retry_max if exhausted else delay),
                    'failed' if exhausted else 'pending',
                    type(error).__name__,
                    now,
                    now,
                    'failed' if exhausted else None,
                    job['path'],
                    job['signature'],
                ),
            )
        return attempts, retry_max if exhausted else delay, exhausted

    def retry_job(self, path: str | Path) -> bool:
        now = time.time()
        with self.lock, self.connection:
            cursor = self.connection.execute(
                '''UPDATE jobs SET status='pending', attempts=0, next_attempt=0,
                   last_error=NULL, result_status=NULL, updated_at=?
                   WHERE path=? AND status IN ('failed', 'cancelled', 'pending')''',
                (now, str(path)),
            )
            return cursor.rowcount > 0

    def retry_all_failed(self, folder_id: int | None = None) -> int:
        now = time.time()
        with self.lock, self.connection:
            if folder_id is not None:
                cursor = self.connection.execute(
                    '''UPDATE jobs SET status='pending', attempts=0, next_attempt=0,
                       last_error=NULL, result_status=NULL, updated_at=?
                       WHERE folder_id=? AND (status='failed' OR (status='pending' AND attempts > 0))''',
                    (now, folder_id),
                )
            else:
                cursor = self.connection.execute(
                    '''UPDATE jobs SET status='pending', attempts=0, next_attempt=0,
                       last_error=NULL, result_status=NULL, updated_at=?
                       WHERE status='failed' OR (status='pending' AND attempts > 0)''',
                    (now,),
                )
            return cursor.rowcount

    def cancel_job(self, path: str | Path) -> bool:
        now = time.time()
        with self.lock, self.connection:
            cursor = self.connection.execute(
                '''UPDATE jobs SET status='cancelled', result_status='cancelled', updated_at=?
                   WHERE path=? AND status IN ('pending', 'failed') AND media_key IS NULL''',
                (now, str(path)),
            )
            return cursor.rowcount > 0

    def get(self, path: str | Path) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                '''SELECT j.*, f.name AS folder_name, a.name AS account_name, a.account_hint AS account_hint
                   FROM jobs j
                   LEFT JOIN monitored_folders f ON j.folder_id = f.id
                   LEFT JOIN google_accounts a ON j.account_id = a.id
                   WHERE j.path=?''',
                (str(path),),
            ).fetchone()
            return dict(row) if row else None

    def list_jobs(
        self,
        search: str = '',
        folder_id: int | None = None,
        account_id: int | None = None,
        file_type: str = '',
        status: str = '',
        sort_by: str = 'created_at',
        sort_dir: str = 'desc',
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if search.strip():
            where.append('LOWER(j.path) LIKE ?')
            params.append(f'%{search.strip().lower()}%')
        if folder_id is not None:
            where.append('j.folder_id = ?')
            params.append(folder_id)
        if account_id is not None:
            where.append('j.account_id = ?')
            params.append(account_id)
        if file_type.strip():
            ext = file_type.strip().lower()
            if not ext.startswith('.'):
                ext = f'.{ext}'
            where.append('LOWER(j.file_ext) = ?')
            params.append(ext)
        if status.strip():
            status_val = status.strip().lower()
            if status_val == 'retrying':
                where.append("j.status = 'pending' AND j.attempts > 0")
            elif status_val == 'failed_or_retrying':
                where.append("(j.status = 'failed' OR (j.status = 'pending' AND j.attempts > 0))")
            else:
                where.append('j.status = ?')
                params.append(status_val)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ''
        allowed_sort = {
            'created_at': 'j.created_at',
            'updated_at': 'j.updated_at',
            'file_size': 'j.file_size',
            'attempts': 'j.attempts',
            'next_attempt': 'j.next_attempt',
        }
        order_col = allowed_sort.get(sort_by, 'j.created_at')
        order_dir = 'ASC' if sort_dir.lower() == 'asc' else 'DESC'
        with self.lock:
            total = int(
                self.connection.execute(f'SELECT COUNT(*) FROM jobs j {where_sql}', params).fetchone()[0]
            )
            rows = self.connection.execute(
                f'''SELECT j.*, f.name AS folder_name, a.name AS account_name, a.account_hint AS account_hint
                    FROM jobs j
                    LEFT JOIN monitored_folders f ON j.folder_id = f.id
                    LEFT JOIN google_accounts a ON j.account_id = a.id
                    {where_sql}
                    ORDER BY {order_col} {order_dir}, j.rowid DESC
                    LIMIT ? OFFSET ?''',
                [*params, limit, offset],
            ).fetchall()
            return [dict(row) for row in rows], total

    def queue_stats(self) -> dict[str, Any]:
        with self.lock:
            counts = {'pending': 0, 'done': 0, 'failed': 0, 'cancelled': 0, 'retrying': 0}
            for row in self.connection.execute(
                'SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status'
            ).fetchall():
                if row['status'] in counts:
                    counts[row['status']] = int(row['cnt'])
            counts['retrying'] = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='pending' AND attempts > 0"
                ).fetchone()[0]
            )
            row = self.connection.execute(
                '''SELECT COALESCE(SUM(CASE WHEN status='done' THEN file_size ELSE 0 END), 0) AS uploaded_bytes,
                          COALESCE(SUM(CASE WHEN status='pending' THEN file_size ELSE 0 END), 0) AS pending_bytes,
                          MAX(completed_at) AS last_completed_at,
                          COUNT(*) AS total
                   FROM jobs'''
            ).fetchone()
            skipped_count = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM upload_history WHERE outcome='skipped'"
                ).fetchone()[0]
            )
            return {
                **counts,
                'total': int(row['total']),
                'uploaded_bytes': int(row['uploaded_bytes']),
                'pending_bytes': int(row['pending_bytes']),
                'last_completed_at': row['last_completed_at'],
                'skipped': skipped_count,
            }

    # --- Monitored Folders ---

    @staticmethod
    def _decode_folder(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data['enabled'] = bool(data['enabled'])
        data['paused'] = bool(data['paused'])
        data['recursive'] = bool(data['recursive'])
        data['extensions'] = json.loads(data['extensions'])
        data['exclude_dirs'] = json.loads(data['exclude_dirs'])
        data['exclude_patterns'] = json.loads(data['exclude_patterns'])
        return data

    def list_folders(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                '''SELECT f.*,
                          a.name AS account_name,
                          a.account_hint AS account_hint,
                          a.status AS account_status,
                          COALESCE(SUM(CASE WHEN j.status='pending' THEN 1 ELSE 0 END), 0) AS pending_count,
                          COALESCE(SUM(CASE WHEN j.status='done' THEN 1 ELSE 0 END), 0) AS uploaded_count,
                          COALESCE(SUM(CASE WHEN j.status='failed' THEN 1 ELSE 0 END), 0) AS failed_count
                   FROM monitored_folders f
                   LEFT JOIN google_accounts a ON f.account_id = a.id
                   LEFT JOIN jobs j ON j.folder_id = f.id
                   GROUP BY f.id
                   ORDER BY f.id ASC'''
            ).fetchall()
            return [self._decode_folder(row) for row in rows]

    def get_folder(self, folder_id: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                '''SELECT f.*,
                          a.name AS account_name,
                          a.account_hint AS account_hint,
                          a.status AS account_status,
                          COALESCE(SUM(CASE WHEN j.status='pending' THEN 1 ELSE 0 END), 0) AS pending_count,
                          COALESCE(SUM(CASE WHEN j.status='done' THEN 1 ELSE 0 END), 0) AS uploaded_count,
                          COALESCE(SUM(CASE WHEN j.status='failed' THEN 1 ELSE 0 END), 0) AS failed_count
                   FROM monitored_folders f
                   LEFT JOIN google_accounts a ON f.account_id = a.id
                   LEFT JOIN jobs j ON j.folder_id = f.id
                   WHERE f.id=?
                   GROUP BY f.id''',
                (folder_id,),
            ).fetchone()
            return self._decode_folder(row) if row else None

    def get_folder_by_path(self, path: str | Path) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                'SELECT * FROM monitored_folders WHERE path=?', (str(path),)
            ).fetchone()
            return self._decode_folder(row) if row else None

    def add_folder(
        self,
        name: str,
        path: str | Path,
        enabled: bool = True,
        paused: bool = False,
        recursive: bool = True,
        extensions: Sequence[str] = DEFAULT_EXTENSIONS,
        exclude_dirs: Sequence[str] = (),
        exclude_patterns: Sequence[str] = (),
        min_file_size: int = 1,
        max_file_size: int | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self.lock, self.connection:
            cursor = self.connection.execute(
                '''INSERT INTO monitored_folders(
                       name, path, enabled, paused, recursive, extensions,
                       exclude_dirs, exclude_patterns, min_file_size, max_file_size,
                       account_id, health, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    name.strip(),
                    str(path),
                    int(enabled),
                    int(paused),
                    int(recursive),
                    json.dumps(list(extensions)),
                    json.dumps(list(exclude_dirs)),
                    json.dumps(list(exclude_patterns)),
                    int(min_file_size),
                    int(max_file_size) if max_file_size is not None else None,
                    account_id,
                    'paused' if paused else ('disabled' if not enabled else 'unknown'),
                    now,
                    now,
                ),
            )
            folder_id = int(cursor.lastrowid)
        folder = self.get_folder(folder_id)
        assert folder is not None
        return folder

    def update_folder(self, folder_id: int, **updates: Any) -> dict[str, Any] | None:
        allowed = {
            'name', 'enabled', 'paused', 'recursive', 'extensions',
            'exclude_dirs', 'exclude_patterns', 'min_file_size', 'max_file_size', 'account_id',
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            if key in ('enabled', 'paused', 'recursive'):
                value = int(bool(value))
            elif key in ('extensions', 'exclude_dirs', 'exclude_patterns'):
                value = json.dumps(list(value))
            elif key == 'name':
                value = str(value).strip()
            assignments.append(f'{key}=?')
            values.append(value)
        if not assignments:
            return self.get_folder(folder_id)
        now = time.time()
        assignments.append('updated_at=?')
        values.append(now)
        values.append(folder_id)
        with self.lock, self.connection:
            self.connection.execute(
                f"UPDATE monitored_folders SET {', '.join(assignments)} WHERE id=?",
                values,
            )
            if 'account_id' in updates:
                # Re-route unconfirmed pending/failed jobs in this folder to the newly assigned account.
                self.connection.execute(
                    '''UPDATE jobs SET account_id=?, updated_at=?
                       WHERE folder_id=? AND status IN ('pending', 'failed') AND media_key IS NULL''',
                    (updates['account_id'], now, folder_id),
                )
        return self.get_folder(folder_id)

    def remove_folder(self, folder_id: int) -> bool:
        with self.lock, self.connection:
            # Preserve all queued/completed/history records and never touch files on disk.
            self.connection.execute('UPDATE jobs SET folder_id=NULL WHERE folder_id=?', (folder_id,))
            cursor = self.connection.execute('DELETE FROM monitored_folders WHERE id=?', (folder_id,))
            return cursor.rowcount > 0

    def update_folder_status(
        self,
        folder_id: int,
        *,
        health: str | None = None,
        last_scan_at: float | None = None,
        last_detected_at: float | None = None,
        discovered_count: int | None = None,
        last_error: str | None = None,
        clear_error: bool = False,
    ) -> None:
        assignments: list[str] = []
        values: list[Any] = []
        if health is not None:
            assignments.append('health=?')
            values.append(health)
        if last_scan_at is not None:
            assignments.append('last_scan_at=?')
            values.append(last_scan_at)
        if last_detected_at is not None:
            assignments.append('last_detected_at=?')
            values.append(last_detected_at)
        if discovered_count is not None:
            assignments.append('discovered_count=?')
            values.append(int(discovered_count))
        if clear_error:
            assignments.append('last_error=NULL')
        elif last_error is not None:
            assignments.append('last_error=?')
            values.append(last_error)
        if not assignments:
            return
        assignments.append('updated_at=?')
        values.append(time.time())
        values.append(folder_id)
        with self.lock, self.connection:
            self.connection.execute(
                f"UPDATE monitored_folders SET {', '.join(assignments)} WHERE id=?",
                values,
            )

    # --- Upload History ---

    def record_history(
        self,
        path: str | Path,
        *,
        folder_id: int | None = None,
        folder_name: str | None = None,
        account_id: int | None = None,
        account_name: str | None = None,
        file_size: int = 0,
        outcome: str,
        media_key: str | None = None,
        duration: float | None = None,
        error: str | None = None,
    ) -> int:
        file_path = Path(path)
        now = time.time()
        with self.lock, self.connection:
            cursor = self.connection.execute(
                '''INSERT INTO upload_history(
                       path, filename, folder_id, folder_name, account_id, account_name,
                       file_size, file_ext, outcome, media_key, duration, error, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    str(path),
                    file_path.name,
                    folder_id,
                    folder_name,
                    account_id,
                    account_name,
                    int(file_size),
                    file_path.suffix.lower(),
                    outcome,
                    media_key,
                    duration,
                    error,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_history(
        self,
        search: str = '',
        outcome: str = '',
        folder_id: int | None = None,
        account_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if search.strip():
            where.append('(LOWER(filename) LIKE ? OR LOWER(path) LIKE ?)')
            pattern = f'%{search.strip().lower()}%'
            params.extend([pattern, pattern])
        if outcome.strip():
            where.append('outcome = ?')
            params.append(outcome.strip().lower())
        if folder_id is not None:
            where.append('folder_id = ?')
            params.append(folder_id)
        if account_id is not None:
            where.append('account_id = ?')
            params.append(account_id)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ''
        with self.lock:
            total = int(
                self.connection.execute(
                    f'SELECT COUNT(*) FROM upload_history {where_sql}', params
                ).fetchone()[0]
            )
            rows = self.connection.execute(
                f'''SELECT * FROM upload_history {where_sql}
                    ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?''',
                [*params, limit, offset],
            ).fetchall()
            return [dict(row) for row in rows], total

    # --- Activity Logs ---

    def record_activity(
        self,
        level: str,
        category: str,
        event_type: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        now = time.time()
        serialized = json.dumps(dict(details)) if details else None
        with self.lock, self.connection:
            cursor = self.connection.execute(
                '''INSERT INTO activity_logs(level, category, event_type, message, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (level.lower(), category.lower(), event_type, message, serialized, now),
            )
            row_id = int(cursor.lastrowid)
            if row_id % 250 == 0:
                self.connection.execute(
                    'DELETE FROM activity_logs WHERE id <= ?',
                    (row_id - MAX_ACTIVITY_LOG_ROWS,),
                )
            return row_id

    def list_activity(
        self,
        search: str = '',
        level: str = '',
        category: str = '',
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if search.strip():
            where.append('(LOWER(message) LIKE ? OR LOWER(event_type) LIKE ?)')
            pattern = f'%{search.strip().lower()}%'
            params.extend([pattern, pattern])
        if level.strip():
            where.append('level = ?')
            params.append(level.strip().lower())
        if category.strip():
            where.append('category = ?')
            params.append(category.strip().lower())
        where_sql = f"WHERE {' AND '.join(where)}" if where else ''
        with self.lock:
            total = int(
                self.connection.execute(
                    f'SELECT COUNT(*) FROM activity_logs {where_sql}', params
                ).fetchone()[0]
            )
            rows = self.connection.execute(
                f'''SELECT * FROM activity_logs {where_sql}
                    ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?''',
                [*params, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                entry = dict(row)
                entry['details'] = json.loads(entry['details']) if entry['details'] else None
                items.append(entry)
            return items, total

    # --- Notifications ---

    def upsert_notification(
        self,
        issue_key: str,
        severity: str,
        category: str,
        title: str,
        message: str,
    ) -> tuple[dict[str, Any], bool]:
        now = time.time()
        with self.lock, self.connection:
            existing = self.connection.execute(
                'SELECT * FROM notifications WHERE issue_key=? AND resolved=0 ORDER BY id DESC LIMIT 1',
                (issue_key,),
            ).fetchone()
            if existing is not None:
                self.connection.execute(
                    'UPDATE notifications SET severity=?, title=?, message=?, updated_at=? WHERE id=?',
                    (severity, title, message, now, existing['id']),
                )
                row = self.connection.execute(
                    'SELECT * FROM notifications WHERE id=?', (existing['id'],)
                ).fetchone()
                data = dict(row)
                data['read'] = bool(data['read'])
                data['resolved'] = bool(data['resolved'])
                return data, False
            cursor = self.connection.execute(
                '''INSERT INTO notifications(issue_key, severity, category, title, message, read, resolved, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)''',
                (issue_key, severity, category, title, message, now, now),
            )
            row = self.connection.execute(
                'SELECT * FROM notifications WHERE id=?', (cursor.lastrowid,)
            ).fetchone()
            data = dict(row)
            data['read'] = bool(data['read'])
            data['resolved'] = bool(data['resolved'])
            return data, True

    def resolve_notification(self, issue_key: str) -> int:
        now = time.time()
        with self.lock, self.connection:
            cursor = self.connection.execute(
                'UPDATE notifications SET resolved=1, updated_at=? WHERE issue_key=? AND resolved=0',
                (now, issue_key),
            )
            return cursor.rowcount

    def list_notifications(
        self, include_resolved: bool = False, limit: int = 50
    ) -> tuple[list[dict[str, Any]], int]:
        where_sql = '' if include_resolved else 'WHERE resolved=0'
        with self.lock:
            unread = int(
                self.connection.execute(
                    'SELECT COUNT(*) FROM notifications WHERE read=0 AND resolved=0'
                ).fetchone()[0]
            )
            rows = self.connection.execute(
                f'SELECT * FROM notifications {where_sql} ORDER BY updated_at DESC, id DESC LIMIT ?',
                (limit,),
            ).fetchall()
            items = []
            for row in rows:
                data = dict(row)
                data['read'] = bool(data['read'])
                data['resolved'] = bool(data['resolved'])
                items.append(data)
            return items, unread

    def mark_notification_read(self, notification_id: int) -> bool:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                'UPDATE notifications SET read=1, updated_at=? WHERE id=?',
                (time.time(), notification_id),
            )
            return cursor.rowcount > 0

    def mark_all_notifications_read(self) -> int:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                'UPDATE notifications SET read=1, updated_at=? WHERE read=0',
                (time.time(),),
            )
            return cursor.rowcount

    def dismiss_notification(self, notification_id: int) -> bool:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                'UPDATE notifications SET resolved=1, read=1, updated_at=? WHERE id=?',
                (time.time(), notification_id),
            )
            return cursor.rowcount > 0

    # --- Runtime App Settings ---

    def get_settings(self) -> dict[str, Any]:
        with self.lock:
            rows = self.connection.execute('SELECT key, value FROM app_settings').fetchall()
            result: dict[str, Any] = {}
            for row in rows:
                try:
                    result[row['key']] = json.loads(row['value'])
                except ValueError:
                    continue
            return result

    def set_settings(self, values: Mapping[str, Any]) -> None:
        now = time.time()
        with self.lock, self.connection:
            for key, value in values.items():
                self.connection.execute(
                    'INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)',
                    (key, json.dumps(value), now),
                )

    def close(self) -> None:
        with self.lock:
            self.connection.close()
