import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from script_gphoto import FileProcessor
from upload_state import StateStore


class DurableQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'photo.jpg'
        self.path.write_bytes(b'photo')
        self.database = Path(self.temp.name) / 'state.db'
        self.store = StateStore(self.database)
        self.addCleanup(lambda: self.store.close())
        self.client = Mock(upload=Mock(return_value={self.path.as_posix(): 'key'}))
        self.processor = FileProcessor(self.client, self.store, stable_seconds=0,
                                       retry_base=0, retry_max=0)

    def reopen(self):
        self.store.close()
        self.store = StateStore(self.database)
        self.processor.store = self.store

    def test_pending_work_survives_restart(self):
        self.processor.submit(self.path)
        self.reopen()
        self.processor.process_once()
        self.assertEqual(self.store.get(self.path)['status'], 'done')
        self.client.upload.assert_called_once()

    def test_completed_work_is_not_uploaded_after_restart(self):
        self.processor.submit(self.path)
        self.processor.process_once()
        self.reopen()
        self.processor.submit(self.path)
        self.assertFalse(self.processor.process_once())
        self.client.upload.assert_called_once()

    def test_network_failure_is_retried_without_deleting(self):
        self.client.upload.side_effect = [ConnectionError('offline'), {self.path.as_posix(): 'key'}]
        self.processor.submit(self.path)
        self.processor.process_once()
        self.assertTrue(self.path.exists())
        self.assertEqual(self.store.get(self.path)['attempts'], 1)
        self.processor.process_once()
        self.assertEqual(self.store.get(self.path)['status'], 'done')

    def test_cleanup_retry_does_not_upload_again_even_after_restart(self):
        self.processor.delete_after_upload = True
        self.processor.submit(self.path)
        with patch.object(Path, 'unlink', side_effect=PermissionError('locked')):
            self.processor.process_once()
        self.assertTrue(self.path.exists())
        self.assertEqual(self.store.get(self.path)['media_key'], 'key')
        self.reopen()
        self.processor.process_once()
        self.assertFalse(self.path.exists())
        self.client.upload.assert_called_once()

    def test_replacement_after_confirmation_is_retained_and_requeued(self):
        self.processor.delete_after_upload = True
        original_mark = self.store.mark_uploaded

        def mark_then_replace(job, signature, key):
            marked = original_mark(job, signature, key)
            self.path.write_bytes(b'new photo not yet uploaded')
            self.processor.submit(self.path)
            return marked

        self.processor.submit(self.path)
        with patch.object(self.store, 'mark_uploaded', side_effect=mark_then_replace):
            self.processor.process_once()
        self.assertEqual(self.path.read_bytes(), b'new photo not yet uploaded')
        job = self.store.get(self.path)
        self.assertIsNone(job['media_key'])
        self.assertEqual(job['status'], 'pending')

    def test_exhausted_job_is_reactivated_by_reconciliation(self):
        self.processor.max_attempts = 1
        self.client.upload.side_effect = ConnectionError('offline')
        self.processor.submit(self.path)
        self.processor.process_once()
        self.assertEqual(self.store.get(self.path)['status'], 'failed')
        self.processor.submit(self.path)
        self.assertEqual(self.store.get(self.path)['status'], 'pending')

    def test_new_scope_requires_fresh_confirmation(self):
        self.store.close()
        self.store = StateStore(self.database, scope='account-one')
        self.processor.store = self.store
        self.processor.submit(self.path)
        self.processor.process_once()
        self.store.close()
        self.store = StateStore(self.database, scope='account-two')
        self.processor.store = self.store
        self.processor.submit(self.path)
        self.processor.process_once()
        self.assertEqual(self.client.upload.call_count, 2)

    def test_pending_cleanup_cannot_delete_outside_watched_root(self):
        self.processor.submit(self.path)
        job = self.store.next_job()
        from script_gphoto import file_signature
        self.store.mark_uploaded(job, file_signature(self.path), 'key')
        self.processor.root = self.path.parent / 'different-root'
        self.processor.delete_after_upload = True
        self.processor.process_once()
        self.assertTrue(self.path.exists())
        self.client.upload.assert_not_called()
