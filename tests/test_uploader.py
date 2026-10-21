import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import Mock

from watchdog.events import FileCreatedEvent, FileMovedEvent

from script_gphoto import (
    FileChangedError,
    FileProcessor,
    PhotoHandler,
    discover_files,
    wait_until_stable,
)
from script_gphoto import (
    upload_file as real_upload_file,
)
from upload_state import StateStore

upload_file = partial(real_upload_file, stable_seconds=0)


class UploadSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'photo.jpg'
        self.path.write_bytes(b'original photo')

    def test_exception_preserves_original(self):
        client = Mock()
        client.upload.side_effect = ConnectionError('offline')
        with self.assertRaises(ConnectionError):
            upload_file(client, self.path, delete_after_upload=True)
        self.assertEqual(self.path.read_bytes(), b'original photo')

    def test_unconfirmed_result_preserves_original(self):
        for result in ({}, None, {'other.jpg': 'key'}, {self.path.as_posix(): ''}):
            with self.subTest(result=result):
                with self.assertRaises(RuntimeError):
                    upload_file(Mock(upload=Mock(return_value=result)), self.path, True)
                self.assertTrue(self.path.exists())

    def test_success_preserves_original_by_default(self):
        client = Mock(upload=Mock(return_value={self.path.as_posix(): 'media-key'}))
        self.assertEqual(upload_file(client, self.path), 'media-key')
        self.assertTrue(self.path.exists())

    def test_confirmed_success_can_delete_original(self):
        client = Mock(upload=Mock(return_value={self.path.as_posix(): 'media-key'}))
        upload_file(client, self.path, delete_after_upload=True)
        self.assertFalse(self.path.exists())

    def test_file_changed_during_upload_is_retained(self):
        def upload(**kwargs):
            self.path.write_bytes(b'new larger photo still being copied')
            return {self.path.as_posix(): 'media-key'}
        with self.assertRaises(FileChangedError):
            upload_file(Mock(upload=upload), self.path, delete_after_upload=True)
        self.assertEqual(self.path.read_bytes(), b'new larger photo still being copied')

    def test_unstable_file_times_out_without_upload(self):
        with self.assertRaises(TimeoutError):
            wait_until_stable(self.path, stable_seconds=10, timeout=0.01)
        self.assertTrue(self.path.exists())

    def test_empty_file_is_not_ready(self):
        self.path.write_bytes(b'')
        with self.assertRaises(TimeoutError):
            wait_until_stable(self.path, stable_seconds=0, timeout=0.01)

    def test_stability_wait_can_be_cancelled(self):
        from threading import Event
        stop = Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            wait_until_stable(self.path, stop_event=stop)

    def test_initial_scan_finds_existing_media_recursively(self):
        nested = self.path.parent / 'nested'
        nested.mkdir()
        video = nested / 'VIDEO.MP4'
        video.write_bytes(b'video')
        (nested / 'copy.tmp').write_bytes(b'in progress')
        self.assertEqual(set(discover_files(self.path.parent)), {self.path, video})

    def test_rename_to_supported_extension_is_submitted(self):
        submit = Mock()
        handler = PhotoHandler(submit, self.path.parent)
        handler.dispatch(FileMovedEvent(str(self.path.with_suffix('.tmp')), str(self.path)))
        submit.assert_called_once_with(self.path)

    def test_outside_folder_is_not_submitted(self):
        submit = Mock()
        PhotoHandler(submit, self.path.parent).dispatch(FileCreatedEvent('/outside/photo.jpg'))
        submit.assert_not_called()

    def test_reconciliation_skips_completed_unchanged_files(self):
        client = Mock(upload=Mock(return_value={self.path.as_posix(): 'key'}))
        store = StateStore(':memory:')
        self.addCleanup(store.close)
        processor = FileProcessor(client, store, stable_seconds=0)
        processor.submit(self.path)
        processor.process_once()
        processor.submit(self.path)
        processor.process_once()
        self.assertEqual(client.upload.call_count, 1)
        self.path.write_bytes(b'changed photo')
        processor.submit(self.path)
        processor.process_once()
        self.assertEqual(client.upload.call_count, 2)


if __name__ == '__main__':
    unittest.main()
