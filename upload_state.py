"""Durable, single-worker upload queue. Keep this database on local storage."""

import json
import sqlite3
import time
from threading import RLock


class StateStore:
    def __init__(self, path):
        self.lock = RLock()
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS jobs (
            path TEXT PRIMARY KEY, signature TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', media_key TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT
        )''')
        self.connection.execute('CREATE INDEX IF NOT EXISTS due_jobs ON jobs(status, next_attempt)')
        self.connection.commit()

    def enqueue(self, path, signature):
        signature = json.dumps(signature)
        with self.lock, self.connection:
            existing = self.connection.execute('SELECT * FROM jobs WHERE path=?', (str(path),)).fetchone()
            if existing and existing['signature'] == signature:
                if existing['status'] == 'failed' and existing['next_attempt'] <= time.time():
                    self.connection.execute(
                        "UPDATE jobs SET status='pending', attempts=0 WHERE path=?", (str(path),))
                return
            self.connection.execute('''INSERT INTO jobs(path, signature) VALUES (?, ?)
                ON CONFLICT(path) DO UPDATE SET signature=excluded.signature,
                status='pending', media_key=NULL, attempts=0, next_attempt=0, last_error=NULL''',
                (str(path), signature))

    def next_job(self):
        with self.lock:
            row = self.connection.execute('''SELECT * FROM jobs
                WHERE status='pending' AND next_attempt<=? ORDER BY next_attempt, rowid LIMIT 1''',
                (time.time(),)).fetchone()
            return dict(row) if row else None

    def mark_uploaded(self, job, signature, media_key):
        signature = json.dumps(signature)
        with self.lock, self.connection:
            cursor = self.connection.execute('''UPDATE jobs SET signature=?, media_key=?,
                attempts=0, next_attempt=0, last_error=NULL
                WHERE path=? AND signature IN (?, ?)''',
                (signature, media_key, job['path'], job['signature'], signature))
            return cursor.rowcount > 0

    def complete(self, path, signature):
        with self.lock, self.connection:
            self.connection.execute("UPDATE jobs SET status='done', last_error=NULL WHERE path=? AND signature=?",
                                    (str(path), signature))

    def fail(self, job, error, max_attempts, retry_base, retry_max):
        attempts = job['attempts'] + 1
        delay = min(retry_max, retry_base * 2 ** min(attempts - 1, 20))
        exhausted = attempts >= max_attempts
        with self.lock, self.connection:
            self.connection.execute('''UPDATE jobs SET attempts=?, next_attempt=?,
                status=?, last_error=? WHERE path=? AND signature=?''',
                (attempts, time.time() + (retry_max if exhausted else delay),
                 'failed' if exhausted else 'pending', type(error).__name__, job['path'], job['signature']))
        return attempts, retry_max if exhausted else delay, exhausted

    def get(self, path):
        with self.lock:
            row = self.connection.execute('SELECT * FROM jobs WHERE path=?', (str(path),)).fetchone()
            return dict(row) if row else None

    def close(self):
        with self.lock:
            self.connection.close()
