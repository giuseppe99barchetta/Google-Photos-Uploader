import json
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

    def test_retry_delay_does_not_block_other_ready_files(self):
        self.processor.retry_base = 5
        self.processor.retry_max = 60
        second = self.path.with_name('second.jpg')
        second.write_bytes(b'second photo')
        self.client.upload.side_effect = [ConnectionError('offline'),
                                          {second.as_posix(): 'second-key'},
                                          {self.path.as_posix(): 'first-key'}]
        with patch('upload_state.time.time', return_value=100):
            self.processor.submit(self.path)
            self.processor.process_once()
            self.processor.submit(second)
            self.processor.process_once()
            self.assertEqual(self.store.get(second)['status'], 'done')
            self.assertEqual(self.store.get(self.path)['next_attempt'], 105)
            self.assertFalse(self.processor.process_once())
        with patch('upload_state.time.time', return_value=105):
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

    def test_change_before_deferred_cleanup_keeps_file_and_requeues(self):
        self.processor.delete_after_upload = True
        self.processor.submit(self.path)
        with patch.object(Path, 'unlink', side_effect=PermissionError('locked')):
            self.processor.process_once()
        self.assertEqual(self.store.get(self.path)['media_key'], 'key')
        self.path.write_bytes(b'changed photo, not uploaded yet')
        self.reopen()
        self.processor.process_once()
        self.assertEqual(self.path.read_bytes(), b'changed photo, not uploaded yet')
        job = self.store.get(self.path)
        self.assertEqual(job['status'], 'pending')
        self.assertIsNone(job['media_key'])
        self.client.upload.assert_called_once()

    def write_old_signature(self, media_key, status):
        # Rows written before ctime was dropped carry a fifth signature field.
        info = self.path.lstat()
        old = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        with self.store.connection:
            self.store.connection.execute(
                'INSERT INTO jobs(path, signature, status, media_key) VALUES (?, ?, ?, ?)',
                (str(self.path.absolute()), json.dumps(old), status, media_key))
            # Simulate a database created before the migration marker existed.
            self.store.connection.execute("DELETE FROM metadata WHERE key='signature_fields'")
        self.reopen()

    def test_done_row_with_old_signature_is_not_uploaded_again(self):
        self.write_old_signature(None, 'done')
        self.processor.submit(self.path)
        self.assertFalse(self.processor.process_once())
        self.client.upload.assert_not_called()

    def test_cleanup_row_with_old_signature_keeps_its_confirmation(self):
        self.write_old_signature('key', 'pending')
        self.processor.submit(self.path)
        self.assertEqual(self.store.get(self.path)['media_key'], 'key')
        self.processor.delete_after_upload = True
        self.processor.process_once()
        self.assertFalse(self.path.exists())
        self.client.upload.assert_not_called()

    def test_hidden_jobs_from_older_versions_are_closed_without_upload_or_delete(self):
        from script_gphoto import file_signature
        hidden = Path(self.temp.name) / '#recycle' / 'old.jpg'
        hidden.parent.mkdir()
        hidden.write_bytes(b'deleted photo')
        apple = Path(self.temp.name) / '._photo.jpg'
        apple.write_bytes(b'resource fork')
        self.store.enqueue(hidden, file_signature(hidden))
        self.store.enqueue(apple, file_signature(apple))
        self.store.mark_uploaded(self.store.get(apple), file_signature(apple), 'key')
        self.reopen()
        self.processor.root = Path(self.temp.name).resolve()
        self.processor.delete_after_upload = True
        self.processor.process_once()
        self.processor.process_once()
        self.assertTrue(hidden.exists())
        self.assertTrue(apple.exists())
        self.assertEqual(self.store.get(hidden)['status'], 'done')
        self.assertEqual(self.store.get(apple)['status'], 'done')
        self.client.upload.assert_not_called()

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

    def _make_files(self, names):
        paths = []
        for index, name in enumerate(names):
            item = Path(self.temp.name) / name
            item.write_bytes(f'media-content-{index}'.encode())
            paths.append(item)
        return paths

    def test_successful_batch_uploads_all_supported_media_types(self):
        files = self._make_files([
            'a.jpg', 'b.jpeg', 'c.png', 'd.heic', 'e.mp4', 'f.mov', 'g.mkv',
        ])
        calls = []

        def fake_upload(target, **kwargs):
            calls.append((list(target), kwargs))
            return {Path(p).as_posix(): f'key-{Path(p).name}' for p in target}

        self.client.upload.side_effect = fake_upload
        for item in files:
            self.processor.submit(item)

        self.assertTrue(self.processor.process_once())
        self.assertTrue(self.processor.process_once())
        self.assertFalse(self.processor.process_once())

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], files[:4])
        self.assertEqual(calls[0][1]['threads'], 2)
        self.assertFalse(calls[0][1]['delete_from_host'])
        self.assertFalse(calls[0][1]['show_progress'])
        self.assertIsNotNone(calls[0][1]['progress_callback'])
        self.assertEqual(calls[1][0], files[4:])
        self.assertEqual(calls[1][1]['threads'], 2)
        self.assertFalse(calls[1][1]['delete_from_host'])

        for item in files:
            job = self.store.get(item)
            self.assertEqual(job['status'], 'done')
            self.assertEqual(job['media_key'], f'key-{item.name}')
            self.assertTrue(item.exists())

    def test_partial_batch_failure_records_successes_and_retries_only_failures(self):
        self.processor.retry_base = 10
        self.processor.retry_max = 60
        files = self._make_files(['p1.jpg', 'p2.jpg', 'p3.jpg', 'p4.jpg'])
        seen_batches = []

        def fake_upload(target, progress_callback=None, **kwargs):
            batch = list(target)
            seen_batches.append(batch)
            if len(seen_batches) == 1:
                for item in batch[:2]:
                    progress_callback({'phase': 'complete', 'path': item.as_posix(),
                                       'bytes_completed': 10, 'bytes_total': 10})
                for item in batch[2:]:
                    progress_callback({'phase': 'error', 'path': item.as_posix(),
                                       'bytes_completed': 0, 'bytes_total': 10})
                return {batch[0].as_posix(): 'k1', batch[1].as_posix(): 'k2'}
            return {item.as_posix(): f'retry-{item.name}' for item in batch}

        self.client.upload.side_effect = fake_upload
        with patch('upload_state.time.time', return_value=100):
            for item in files:
                self.processor.submit(item)
            self.processor.process_once()

            self.assertEqual(self.store.get(files[0])['status'], 'done')
            self.assertEqual(self.store.get(files[1])['status'], 'done')
            for failed in files[2:]:
                job = self.store.get(failed)
                self.assertEqual(job['status'], 'pending')
                self.assertIsNone(job['media_key'])
                self.assertEqual(job['attempts'], 1)
                self.assertEqual(job['next_attempt'], 110)
            self.assertFalse(self.processor.process_once())

        with patch('upload_state.time.time', return_value=110):
            self.assertTrue(self.processor.process_once())

        self.assertEqual(seen_batches[1], files[2:])
        for item in files:
            self.assertEqual(self.store.get(item)['status'], 'done')
            self.assertTrue(item.exists())

    def test_progress_complete_without_returned_media_key_is_not_confirmed(self):
        files = self._make_files(['confirmed.jpg', 'unconfirmed.jpg'])

        def fake_upload(target, progress_callback=None, **kwargs):
            for item in target:
                progress_callback({'phase': 'complete', 'path': item.as_posix(),
                                   'bytes_completed': 10, 'bytes_total': 10})
            # Only the first file has a valid media key; the second is omitted.
            return {files[0].as_posix(): 'valid-key'}

        self.client.upload.side_effect = fake_upload
        for item in files:
            self.processor.submit(item)
        self.processor.process_once()

        self.assertEqual(self.store.get(files[0])['status'], 'done')
        unconfirmed = self.store.get(files[1])
        self.assertEqual(unconfirmed['status'], 'pending')
        self.assertIsNone(unconfirmed['media_key'])
        self.assertEqual(unconfirmed['attempts'], 1)

    def test_existing_remote_file_is_confirmed_without_duplicate_upload(self):
        files = self._make_files(['new.jpg', 'already_remote.jpg'])

        def fake_upload(target, progress_callback=None, **kwargs):
            progress_callback({'phase': 'uploading', 'path': files[0].as_posix(),
                               'bytes_completed': 10, 'bytes_total': 10})
            progress_callback({'phase': 'complete', 'path': files[0].as_posix(),
                               'bytes_completed': 10, 'bytes_total': 10})
            progress_callback({'phase': 'skipped', 'path': files[1].as_posix(),
                               'bytes_completed': 10, 'bytes_total': 10})
            return {
                files[0].as_posix(): 'new-key',
                files[1].as_posix(): 'existing-remote-key',
            }

        self.client.upload.side_effect = fake_upload
        for item in files:
            self.processor.submit(item)
        with self.assertLogs('gphotos_uploader', level='INFO') as logs:
            self.processor.process_once()

        self.assertEqual(self.store.get(files[0])['status'], 'done')
        self.assertEqual(self.store.get(files[1])['status'], 'done')
        self.assertEqual(self.store.get(files[1])['media_key'], 'existing-remote-key')
        self.assertTrue(any('status=duplicate' in line for line in logs.output))
        for item in files:
            self.processor.submit(item)
        self.assertFalse(self.processor.process_once())
        self.client.upload.assert_called_once()

    def test_missing_file_before_batch_does_not_block_other_files_and_retries_if_restored(self):
        files = self._make_files(['m1.jpg', 'm2.jpg', 'm3.jpg', 'm4.jpg'])
        seen_batches = []

        def fake_upload(target, **kwargs):
            batch = list(target)
            seen_batches.append(batch)
            return {item.as_posix(): f'key-{item.name}' for item in batch}

        self.client.upload.side_effect = fake_upload
        for item in files:
            self.processor.submit(item)

        files[1].unlink()
        self.processor.process_once()

        self.assertEqual(seen_batches[0], [files[0], files[2], files[3]])
        for present in (files[0], files[2], files[3]):
            self.assertEqual(self.store.get(present)['status'], 'done')
        missing_job = self.store.get(files[1])
        self.assertEqual(missing_job['status'], 'pending')
        self.assertEqual(missing_job['last_error'], 'FileNotFoundError')
        self.assertEqual(missing_job['attempts'], 1)

        # Restore file on disk; next pass requeues new signature and uploads it.
        files[1].write_bytes(b'restored photo')
        self.processor.process_once()
        self.processor.process_once()
        self.assertEqual(self.store.get(files[1])['status'], 'done')

    def test_file_changed_after_queueing_is_requeued_while_other_batch_files_upload(self):
        files = self._make_files(['c1.jpg', 'c2.jpg', 'c3.jpg', 'c4.jpg'])
        seen_batches = []

        def fake_upload(target, **kwargs):
            batch = list(target)
            seen_batches.append(batch)
            return {item.as_posix(): f'key-{item.name}' for item in batch}

        self.client.upload.side_effect = fake_upload
        for item in files:
            self.processor.submit(item)

        files[1].write_bytes(b'updated content after queueing')
        self.processor.process_once()

        self.assertEqual(seen_batches[0], [files[0], files[2], files[3]])
        for unchanged in (files[0], files[2], files[3]):
            self.assertEqual(self.store.get(unchanged)['status'], 'done')
        requeued = self.store.get(files[1])
        self.assertEqual(requeued['status'], 'pending')
        self.assertIsNone(requeued['media_key'])
        self.assertEqual(requeued['attempts'], 0)

        self.processor.process_once()
        self.assertEqual(seen_batches[1], [files[1]])
        self.assertEqual(self.store.get(files[1])['status'], 'done')

    def test_file_changed_during_batch_upload_is_retained_while_others_confirm(self):
        self.processor.delete_after_upload = True
        files = self._make_files(['u1.jpg', 'u2.jpg', 'u3.jpg', 'u4.jpg'])

        def fake_upload(target, **kwargs):
            files[2].write_bytes(b'replaced mid-upload with newer content')
            return {Path(p).as_posix(): f'key-{Path(p).name}' for p in target}

        self.client.upload.side_effect = fake_upload
        for item in files:
            self.processor.submit(item)
        self.processor.process_once()

        for unchanged in (files[0], files[1], files[3]):
            self.assertEqual(self.store.get(unchanged)['status'], 'done')
            self.assertFalse(unchanged.exists())
        self.assertTrue(files[2].exists())
        self.assertEqual(files[2].read_bytes(), b'replaced mid-upload with newer content')
        changed_job = self.store.get(files[2])
        self.assertEqual(changed_job['status'], 'pending')
        self.assertIsNone(changed_job['media_key'])
        self.assertEqual(changed_job['last_error'], 'FileChangedError')

    def test_crash_before_persisting_batch_recovers_via_remote_duplicate_detection(self):
        files = self._make_files(['r1.jpg', 'r2.jpg', 'r3.jpg', 'r4.jpg'])
        for item in files:
            self.processor.submit(item)

        # Simulate container crash mid-batch after Google Photos accepted r1 and r2 remotely,
        # before SQLite recorded any completion.
        self.reopen()
        for item in files:
            self.assertEqual(self.store.get(item)['status'], 'pending')
            self.assertIsNone(self.store.get(item)['media_key'])

        uploaded_fresh = []

        def recovered_upload(target, progress_callback=None, **kwargs):
            result = {}
            for item in target:
                if item in files[:2]:
                    progress_callback({'phase': 'skipped', 'path': item.as_posix(),
                                       'bytes_completed': 10, 'bytes_total': 10})
                    result[item.as_posix()] = f'remote-existing-{item.name}'
                else:
                    uploaded_fresh.append(item)
                    progress_callback({'phase': 'complete', 'path': item.as_posix(),
                                       'bytes_completed': 10, 'bytes_total': 10})
                    result[item.as_posix()] = f'fresh-{item.name}'
            return result

        self.client.upload.side_effect = recovered_upload
        self.processor.process_once()

        self.assertEqual(uploaded_fresh, files[2:])
        for item in files:
            self.assertEqual(self.store.get(item)['status'], 'done')
        self.reopen()
        self.assertFalse(self.processor.process_once())

    def test_next_jobs_returns_distinct_due_jobs_in_order_without_duplicates(self):
        files = self._make_files(['d1.jpg', 'd2.jpg', 'd3.jpg', 'd4.jpg', 'd5.jpg'])
        for item in files:
            self.processor.submit(item)
            self.processor.submit(item)  # Repeated submission of same signature is a no-op.
        batch = self.store.next_jobs(4)
        self.assertEqual([Path(job['path']) for job in batch], files[:4])
        self.assertEqual(len({job['path'] for job in batch}), 4)
        with self.assertRaises(ValueError):
            self.store.next_jobs(0)
