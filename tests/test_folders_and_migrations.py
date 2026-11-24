import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from folder_manager import (
    FolderWatcherPool,
    browse_approved_directories,
    create_dedicated_subfolder,
    discover_folder_files,
    normalize_extensions,
    validate_approved_path,
)
from script_gphoto import FileProcessor
from upload_state import CURRENT_SCHEMA_VERSION, StateStore


class MigrationAndFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root_a = self.base / 'root_a'
        self.root_b = self.base / 'root_b'
        self.outside = self.base / 'outside'
        for directory in (self.root_a, self.root_b, self.outside):
            directory.mkdir()

    def test_v1_database_migrates_with_backup_and_preserves_existing_jobs(self):
        db_path = self.base / 'uploads.db'
        conn = sqlite3.connect(str(db_path))
        conn.execute('''CREATE TABLE jobs (
            path TEXT PRIMARY KEY, signature TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', media_key TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT
        )''')
        conn.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        conn.execute("INSERT INTO metadata VALUES ('scope', 'existing-scope')")
        conn.execute(
            "INSERT INTO jobs VALUES ('/data/old.jpg', '[1,2,3,4,5]', 'done', 'mk-1', 0, 0, NULL)"
        )
        conn.execute(
            "INSERT INTO jobs VALUES ('/data/queued.jpg', '[1,2,9,4,5]', 'pending', NULL, 1, 0, 'TimeoutError')"
        )
        conn.commit()
        conn.close()

        store = StateStore(db_path, scope='existing-scope')
        self.assertEqual(store.schema_version(), CURRENT_SCHEMA_VERSION)
        done_job = store.get('/data/old.jpg')
        pending_job = store.get('/data/queued.jpg')
        self.assertEqual(done_job['status'], 'done')
        self.assertEqual(done_job['media_key'], 'mk-1')
        self.assertEqual(pending_job['status'], 'pending')
        self.assertEqual(pending_job['attempts'], 1)
        backups = list(self.base.glob('uploads.backup-v1-to-v3-*.db'))
        self.assertEqual(len(backups), 1)
        store.close()

        # Reopening after migration is safe and does not duplicate backups.
        reopened = StateStore(db_path, scope='existing-scope')
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version(), CURRENT_SCHEMA_VERSION)
        self.assertEqual(reopened.get('/data/old.jpg')['media_key'], 'mk-1')
        self.assertEqual(len(list(self.base.glob('uploads.backup-v1-to-v3-*.db'))), 1)

    def test_v2_database_migrates_to_v3_with_backup_and_preserves_folders_and_jobs(self):
        db_path = self.base / 'uploads_v2.db'
        conn = sqlite3.connect(str(db_path))
        conn.execute('''CREATE TABLE jobs (
            path TEXT PRIMARY KEY, signature TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', media_key TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT,
            folder_id INTEGER, file_size INTEGER NOT NULL DEFAULT 0,
            file_ext TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0, last_attempt_at REAL,
            completed_at REAL, upload_duration REAL, result_status TEXT
        )''')
        conn.execute('''CREATE TABLE monitored_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1, paused INTEGER NOT NULL DEFAULT 0,
            recursive INTEGER NOT NULL DEFAULT 1, extensions TEXT NOT NULL,
            exclude_dirs TEXT NOT NULL DEFAULT '[]', exclude_patterns TEXT NOT NULL DEFAULT '[]',
            min_file_size INTEGER NOT NULL DEFAULT 1, max_file_size INTEGER,
            last_scan_at REAL, last_detected_at REAL, discovered_count INTEGER NOT NULL DEFAULT 0,
            health TEXT NOT NULL DEFAULT 'unknown', last_error TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )''')
        conn.execute('''CREATE TABLE upload_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, filename TEXT NOT NULL,
            folder_id INTEGER, folder_name TEXT, file_size INTEGER NOT NULL DEFAULT 0,
            file_ext TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL, media_key TEXT,
            duration REAL, error TEXT, created_at REAL NOT NULL
        )''')
        conn.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        conn.execute("INSERT INTO metadata VALUES ('schema_version', '2')")
        conn.execute("INSERT INTO metadata VALUES ('scope', 'v2-scope')")
        conn.execute(
            "INSERT INTO monitored_folders(name, path, extensions, created_at, updated_at) "
            "VALUES ('data', '/data', '[\".jpg\"]', 1, 1)"
        )
        conn.execute(
            "INSERT INTO jobs(path, signature, status, media_key, folder_id) "
            "VALUES ('/data/a.jpg', '[1,2,3,4,5]', 'done', 'mk-v2', 1)"
        )
        conn.commit()
        conn.close()

        store = StateStore(db_path, scope='v2-scope')
        self.addCleanup(store.close)
        self.assertEqual(store.schema_version(), 3)
        self.assertEqual(store.get('/data/a.jpg')['media_key'], 'mk-v2')
        self.assertEqual(len(store.list_folders()), 1)
        self.assertEqual(len(list(self.base.glob('uploads_v2.backup-v2-to-v3-*.db'))), 1)

    def test_create_dedicated_subfolder_validates_names_and_approved_roots(self):
        created = create_dedicated_subfolder(self.root_a, 'account-a', [self.root_a, self.root_b])
        self.assertTrue(created.is_dir())
        self.assertEqual(created, (self.root_a / 'account-a').resolve())

        for bad_name in ('', '../escape', 'a/b', 'a\\b', '.hidden', '..'):
            with self.subTest(bad_name=bad_name):
                with self.assertRaises(ValueError):
                    create_dedicated_subfolder(self.root_a, bad_name, [self.root_a])

        with self.assertRaises(PermissionError):
            create_dedicated_subfolder(self.outside, 'account-b', [self.root_a])

    def test_approved_root_validation_and_traversal_rejection(self):
        nested = self.root_a / 'vacations' / '2026'
        nested.mkdir(parents=True)
        approved = [self.root_a, self.root_b]

        self.assertEqual(validate_approved_path(self.root_a, approved, require_exists=True), self.root_a.resolve())
        self.assertEqual(validate_approved_path(nested, approved, require_exists=True), nested.resolve())

        with self.assertRaises(PermissionError):
            validate_approved_path(self.outside, approved)
        with self.assertRaises(PermissionError):
            validate_approved_path(str(self.root_a) + '/../outside', approved)
        with self.assertRaises(PermissionError):
            validate_approved_path('relative/path', approved)
        with self.assertRaises(ValueError):
            validate_approved_path('', approved)
        with self.assertRaises(ValueError):
            validate_approved_path('/data/bad\x00path', approved)
        if os.name != 'nt':
            with self.assertRaises(PermissionError):
                validate_approved_path('C:\\Users\\test\\Photos', approved)
            symlink = self.root_a / 'escape_link'
            symlink.symlink_to(self.outside)
            with self.assertRaises(PermissionError):
                validate_approved_path(symlink, approved)

    def test_browse_approved_directories_stays_within_approved_roots(self):
        sub1 = self.root_a / 'album_one'
        sub2 = self.root_a / 'album_two'
        sub1.mkdir()
        sub2.mkdir()
        if os.name != 'nt':
            (self.root_a / 'link_outside').symlink_to(self.outside)

        top = browse_approved_directories(None, [self.root_a, self.root_b])
        self.assertIsNone(top['current_path'])
        self.assertIsNone(top['parent_path'])
        self.assertEqual(len(top['directories']), 2)

        inside = browse_approved_directories(str(self.root_a), [self.root_a, self.root_b])
        self.assertEqual(inside['current_path'], str(self.root_a.resolve()))
        self.assertIsNone(inside['parent_path'])
        self.assertEqual([d['name'] for d in inside['directories']], ['album_one', 'album_two'])

        child = browse_approved_directories(str(sub1), [self.root_a, self.root_b])
        self.assertEqual(child['parent_path'], str(self.root_a.resolve()))

        with self.assertRaises(PermissionError):
            browse_approved_directories(str(self.outside), [self.root_a, self.root_b])

    def test_folder_filters_recursive_excludes_and_size_bounds(self):
        (self.root_a / 'keep.jpg').write_bytes(b'x' * 50)
        (self.root_a / 'small.jpg').write_bytes(b'x' * 5)
        (self.root_a / 'huge.jpg').write_bytes(b'x' * 500)
        (self.root_a / '._hidden.jpg').write_bytes(b'x' * 50)
        (self.root_a / 'video.mov').write_bytes(b'x' * 50)
        sub = self.root_a / 'nested'
        sub.mkdir()
        (sub / 'nested.heic').write_bytes(b'x' * 50)
        ignored = self.root_a / '@eaDir'
        ignored.mkdir()
        (ignored / 'thumb.jpg').write_bytes(b'x' * 50)

        folder = {
            'path': str(self.root_a),
            'recursive': True,
            'extensions': ['.jpg', '.heic'],
            'exclude_dirs': ['@eaDir'],
            'exclude_patterns': ['._*'],
            'min_file_size': 10,
            'max_file_size': 200,
        }
        found = sorted(p.name for p in discover_folder_files(folder))
        self.assertEqual(found, ['keep.jpg', 'nested.heic'])

        folder_non_recursive = {**folder, 'recursive': False}
        found_top = sorted(p.name for p in discover_folder_files(folder_non_recursive))
        self.assertEqual(found_top, ['keep.jpg'])

        with self.assertRaises(ValueError):
            normalize_extensions(['.jpg', '.exe'])

    def test_multiple_folders_pause_and_remove_preserve_files_and_queue(self):
        photo_a = self.root_a / 'a.jpg'
        photo_b = self.root_b / 'b.png'
        photo_a.write_bytes(b'photo a')
        photo_b.write_bytes(b'photo b')

        store = StateStore(self.base / 'multi.db')
        self.addCleanup(store.close)
        folder_a = store.add_folder(name='Folder A', path=self.root_a)
        folder_b = store.add_folder(name='Folder B', path=self.root_b)

        client = Mock()
        client.upload.side_effect = lambda target, **kwargs: {
            Path(p).as_posix(): f'key-{Path(p).name}' for p in target
        }
        processor = FileProcessor(
            client, store, allowed_roots=[self.root_a, self.root_b],
            stable_seconds=0, retry_base=0, retry_max=0,
        )
        pool = FolderWatcherPool(processor.submit, poll_interval=0.05)
        self.addCleanup(pool.stop_all)

        scan_a = pool.scan_folder(folder_a)
        scan_b = pool.scan_folder(folder_b)
        self.assertEqual(scan_a['newly_queued'], 1)
        self.assertEqual(scan_b['newly_queued'], 1)

        # Pause folder A: only folder B should be picked up by next_jobs.
        store.update_folder(folder_a['id'], paused=True)
        self.assertTrue(processor.process_once())
        self.assertEqual(store.get(photo_b)['status'], 'done')
        self.assertEqual(store.get(photo_a)['status'], 'pending')

        # Removing folder A must not delete photo_a or erase its queued record.
        self.assertTrue(store.remove_folder(folder_a['id']))
        self.assertTrue(photo_a.exists())
        job_a = store.get(photo_a)
        self.assertIsNotNone(job_a)
        self.assertIsNone(job_a['folder_id'])

    def test_deduplicated_notifications_do_not_repeat_for_same_issue(self):
        store = StateStore(':memory:')
        self.addCleanup(store.close)
        n1, created1 = store.upsert_notification('folder:1:unavailable', 'warning', 'folder', 'Folder offline', 'Cannot access /data')
        n2, created2 = store.upsert_notification('folder:1:unavailable', 'warning', 'folder', 'Folder offline', 'Still cannot access /data')
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(n1['id'], n2['id'])
        items, unread = store.list_notifications()
        self.assertEqual(len(items), 1)
        self.assertEqual(unread, 1)

        store.resolve_notification('folder:1:unavailable')
        items_after, unread_after = store.list_notifications()
        self.assertEqual(items_after, [])
        self.assertEqual(unread_after, 0)


if __name__ == '__main__':
    unittest.main()
