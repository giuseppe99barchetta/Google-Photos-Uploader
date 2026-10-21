import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from script_gphoto import upload_file


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


if __name__ == '__main__':
    unittest.main()
