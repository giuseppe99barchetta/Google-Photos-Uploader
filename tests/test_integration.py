import signal
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch

from test_config import FAKE_AUTH

from script_gphoto import FileProcessor, WatchSupervisor, discover_files, upload_file
from upload_state import StateStore


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            Event().wait(0.02)
        self.fail('Timed out waiting for the polling observer and worker')

    def test_initial_scan_and_atomic_rename_reach_real_worker(self):
        photos = self.root / 'photos'
        photos.mkdir()
        existing = photos / 'existing.jpg'
        existing.write_bytes(b'preexisting photo')
        store = StateStore(self.root / 'state.db')
        self.addCleanup(store.close)
        client = Mock()
        client.upload.side_effect = lambda target, **kwargs: {Path(target).as_posix(): 'key'}
        processor = FileProcessor(client, store, stable_seconds=0)
        worker = Thread(target=processor.run, daemon=True)
        supervisor = WatchSupervisor(photos, processor.submit, poll_interval=0.05)
        try:
            worker.start()
            self.assertTrue(supervisor.check())
            for path in discover_files(photos):
                processor.submit(path)
            self.wait_for(lambda: store.get(existing)['status'] == 'done')
            temporary = photos / 'copy.tmp'
            temporary.write_bytes(b'completed video')
            video = photos / 'video.mp4'
            temporary.rename(video)
            self.wait_for(lambda: store.get(video) and store.get(video)['status'] == 'done')
            self.assertTrue(existing.exists())
            self.assertTrue(video.exists())
            self.assertEqual(client.upload.call_count, 2)
        finally:
            processor.stop_event.set()
            supervisor.stop()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())

    def test_real_gpmc_failure_result_preserves_original(self):
        # Importing GPMC changes SIGINT; restore the test runner's handler.
        previous = signal.getsignal(signal.SIGINT)
        try:
            from gpmc import Client
        finally:
            signal.signal(signal.SIGINT, previous)
        photo = self.root / 'photo.jpg'
        photo.write_bytes(b'photo')
        client = Client(auth_data=FAKE_AUTH, log_level='CRITICAL')
        with patch.object(client, '_upload_file', side_effect=ConnectionError('offline')):
            with self.assertRaises(RuntimeError):
                upload_file(client, photo, delete_after_upload=True, stable_seconds=0)
        self.assertTrue(photo.exists())
