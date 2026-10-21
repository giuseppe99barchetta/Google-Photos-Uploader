import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import Mock

from script_gphoto import FileChangedError, upload_file as real_upload_file, wait_until_stable

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


if __name__ == '__main__':
    unittest.main()
