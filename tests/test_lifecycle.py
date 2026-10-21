import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch

from test_config import FAKE_AUTH

import script_gphoto
from script_gphoto import (
    FileProcessor,
    RedactingFormatter,
    ShutdownTimeout,
    healthcheck,
    install_signal_handlers,
    main,
    run_service,
)
from upload_state import StateStore
from uploader_config import Config


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Config resolves this path before enqueueing. Windows TEMP can use
        # short names or casing aliases that SQLite string keys do not equate.
        self.base = Path(self.temp.name).resolve()
        self.photos = self.base / 'photos'
        self.photos.mkdir()
        self.state = self.base / 'state'
        self.env = {**os.environ, 'AUTH_DATA': FAKE_AUTH, 'AUTH_DATA_FILE': '',
                    'WATCHED_FOLDER': str(self.photos), 'STATE_DIR': str(self.state),
                    'STABLE_SECONDS': '0.01', 'SHUTDOWN_TIMEOUT': '2'}

    def test_shutdown_after_confirmation_defers_cleanup_and_resumes_it(self):
        photo = self.photos / 'photo.jpg'
        photo.write_bytes(b'photo')
        stop = Event()
        client = Mock()

        def upload(target, **kwargs):
            stop.set()
            return {Path(target).as_posix(): 'key'}

        client.upload.side_effect = upload
        config = Config.from_env({**self.env, 'DELETE_AFTER_UPLOAD': 'true'})
        run_service(config, Mock(return_value=client), stop)
        self.assertTrue(photo.exists())
        self.assertFalse(healthcheck(self.state))
        store = StateStore(self.state / 'uploads.db')
        self.addCleanup(store.close)
        job = store.get(photo)
        self.assertIsNotNone(job, 'The confirmed upload must remain in the queue after shutdown')
        self.assertEqual(job['media_key'], 'key')
        self.assertEqual(job['status'], 'pending')
        FileProcessor(client, store, delete_after_upload=True).process_once()
        self.assertFalse(photo.exists())
        client.upload.assert_called_once()

    def test_known_tokens_are_redacted_in_log_messages(self):
        formatter = RedactingFormatter(FAKE_AUTH)
        record = logging.LogRecord('test', logging.ERROR, '', 0,
                                   'Failure: %s', ('fake-token-for-offline-tests-only',), None)
        message = formatter.format(record)
        self.assertNotIn('fake-token', message)
        self.assertIn('[REDACTED]', message)

    def test_shutdown_deadline_leaves_slow_upload_pending(self):
        photo = self.photos / 'slow.jpg'
        photo.write_bytes(b'photo')
        config = Config.from_env({**self.env, 'SHUTDOWN_TIMEOUT': '0.05'})
        stop, release = Event(), Event()
        threads = []
        store = StateStore(self.state / 'uploads.db', scope=config.state_scope)
        client = Mock()

        def slow_upload(target, **kwargs):
            stop.set()
            release.wait(5)
            return {Path(target).as_posix(): 'key'}

        def track_thread(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            threads.append(thread)
            return thread

        client.upload.side_effect = slow_upload
        try:
            with patch('script_gphoto.StateStore', return_value=store):
                with patch('script_gphoto.Thread', side_effect=track_thread):
                    with self.assertRaises(ShutdownTimeout):
                        run_service(config, Mock(return_value=client), stop)
            self.assertTrue(photo.exists())
            self.assertFalse(healthcheck(self.state))
            job = store.get(photo)
            self.assertIsNotNone(job, 'The in-flight upload must remain queued after the shutdown deadline')
            self.assertEqual(job['status'], 'pending')
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=5)
            store.close()

    def test_signals_request_an_orderly_stop(self):
        stop = Event()
        previous = install_signal_handlers(stop)
        try:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            self.assertTrue(stop.is_set())
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    def test_invalid_config_exits_before_client_initialization(self):
        with patch.dict(os.environ, {'AUTH_DATA': ''}, clear=True):
            with self.assertLogs('gphotos_uploader', level='ERROR'):
                self.assertEqual(main(), 2)

    @unittest.skipUnless(os.name == 'posix', 'POSIX SIGTERM behavior is verified on Linux CI')
    def test_real_sigterm_exits_cleanly(self):
        process = subprocess.Popen(
            [sys.executable, '-B', str(Path(script_gphoto.__file__).resolve())], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not healthcheck(self.state) and time.monotonic() < deadline and process.poll() is None:
                Event().wait(0.05)
            self.assertTrue(healthcheck(self.state))
            process.terminate()
            _, errors = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, errors)
            self.assertFalse(healthcheck(self.state))
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
