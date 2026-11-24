import signal
import tempfile
import time
import unittest
from pathlib import Path
from threading import Barrier, Event, Thread
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
        client.upload.side_effect = lambda target, **kwargs: {
            Path(p).as_posix(): 'key' for p in target
        }
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
                upload_file(client, photo, stable_seconds=0)
        self.assertTrue(photo.exists())

    def test_real_gpmc_concurrent_batch_and_partial_failure_recovery(self):
        previous = signal.getsignal(signal.SIGINT)
        try:
            from gpmc import Client
        finally:
            signal.signal(signal.SIGINT, previous)
        files = []
        for name in ('first.jpg', 'second.png', 'third.heic', 'fourth.mov'):
            item = self.root / name
            item.write_bytes(f'bytes-for-{name}'.encode())
            files.append(item)
        store = StateStore(self.root / 'batch.db')
        self.addCleanup(store.close)
        client = Client(auth_data=FAKE_AUTH, log_level='CRITICAL')
        processor = FileProcessor(
            client, store, stable_seconds=0, retry_base=0, retry_max=0,
            upload_threads=2, upload_batch_size=4,
        )
        for item in files:
            processor.submit(item)

        barrier = Barrier(2)
        should_fail_fourth = [True]

        def fake_upload_file(file_path, *args, progress_callback=None, delete_from_host=False, **kwargs):
            self.assertFalse(delete_from_host)
            path = Path(file_path).absolute()
            if path in files[:2]:
                if progress_callback:
                    progress_callback({
                        'version': 1, 'type': 'progress', 'operation': 'upload',
                        'phase': 'uploading', 'path': path.as_posix(),
                        'filename': path.name, 'bytes_completed': 5, 'bytes_total': 10,
                    })
                # Both worker threads must overlap here, proving concurrent execution.
                barrier.wait(timeout=5)
                if progress_callback:
                    progress_callback({
                        'version': 1, 'type': 'progress', 'operation': 'upload',
                        'phase': 'complete', 'path': path.as_posix(),
                        'filename': path.name, 'bytes_completed': 10, 'bytes_total': 10,
                    })
                return {path.as_posix(): f'key-{path.name}'}
            if path == files[2]:
                if progress_callback:
                    progress_callback({
                        'version': 1, 'type': 'progress', 'operation': 'upload',
                        'phase': 'skipped', 'path': path.as_posix(),
                        'filename': path.name, 'bytes_completed': 10, 'bytes_total': 10,
                    })
                return {path.as_posix(): 'existing-heic-key'}
            if should_fail_fourth[0]:
                raise ConnectionError('transient upload failure')
            return {path.as_posix(): 'recovered-mov-key'}

        with patch.object(client, '_upload_file', side_effect=fake_upload_file):
            self.assertTrue(processor.process_once())
            self.assertEqual(store.get(files[0])['status'], 'done')
            self.assertEqual(store.get(files[1])['status'], 'done')
            self.assertEqual(store.get(files[2])['status'], 'done')
            self.assertEqual(store.get(files[2])['media_key'], 'existing-heic-key')
            fourth_job = store.get(files[3])
            self.assertEqual(fourth_job['status'], 'pending')
            self.assertIsNone(fourth_job['media_key'])
            self.assertEqual(fourth_job['attempts'], 1)

            should_fail_fourth[0] = False
            self.assertTrue(processor.process_once())
            self.assertEqual(store.get(files[3])['status'], 'done')
            self.assertEqual(store.get(files[3])['media_key'], 'recovered-mov-key')
            self.assertFalse(processor.process_once())
        for item in files:
            self.assertTrue(item.exists())

    def test_real_gpmc_hash_duplicate_detection_confirms_without_uploading(self):
        previous = signal.getsignal(signal.SIGINT)
        try:
            from gpmc import Client
        finally:
            signal.signal(signal.SIGINT, previous)
        photo = self.root / 'already_there.heic'
        photo.write_bytes(b'heic photo bytes already in google photos')
        store = StateStore(self.root / 'dedup.db')
        self.addCleanup(store.close)
        client = Client(auth_data=FAKE_AUTH, log_level='CRITICAL')
        processor = FileProcessor(client, store, stable_seconds=0)
        processor.submit(photo)

        with (
            patch.object(client.api, 'find_remote_media_by_hash', return_value='remote-dedup-key') as find_hash,
            patch.object(client.api, 'get_upload_token') as get_token,
            patch.object(client.api, 'upload_file') as api_upload,
            patch.object(client.api, 'commit_upload') as commit,
        ):
            self.assertTrue(processor.process_once())

        find_hash.assert_called_once()
        get_token.assert_not_called()
        api_upload.assert_not_called()
        commit.assert_not_called()
        job = store.get(photo)
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['media_key'], 'remote-dedup-key')
        self.assertTrue(photo.exists())
