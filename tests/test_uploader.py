import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import Mock, patch

from watchdog.events import FileCreatedEvent, FileMovedEvent

from script_gphoto import (
    BatchProgressTracker,
    FileChangedError,
    FileProcessor,
    PhotoHandler,
    discover_files,
    file_signature,
    wait_until_stable,
    wait_until_stable_batch,
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
            upload_file(client, self.path)
        self.assertEqual(self.path.read_bytes(), b'original photo')

    def test_unconfirmed_result_preserves_original(self):
        for result in ({}, None, {'other.jpg': 'key'}, {self.path.as_posix(): ''}):
            with self.subTest(result=result):
                with self.assertRaises(RuntimeError):
                    upload_file(Mock(upload=Mock(return_value=result)), self.path)
                self.assertTrue(self.path.exists())

    def test_success_preserves_original_by_default(self):
        client = Mock(upload=Mock(return_value={self.path.as_posix(): 'media-key'}))
        self.assertEqual(upload_file(client, self.path), 'media-key')
        self.assertTrue(self.path.exists())

    def test_confirmed_success_can_delete_original(self):
        client = Mock(upload=Mock(return_value={self.path.as_posix(): 'media-key'}))
        store = StateStore(':memory:')
        self.addCleanup(store.close)
        processor = FileProcessor(client, store, delete_after_upload=True, stable_seconds=0)
        processor.submit(self.path)
        processor.process_once()
        self.assertFalse(self.path.exists())

    def test_file_changed_during_upload_is_retained(self):
        def upload(**kwargs):
            self.path.write_bytes(b'new larger photo still being copied')
            return {self.path.as_posix(): 'media-key'}
        with self.assertRaises(FileChangedError):
            upload_file(Mock(upload=upload), self.path)
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
        stop = Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            wait_until_stable(self.path, stop_event=stop)

    def test_each_copy_change_restarts_the_stability_window(self):
        clock = [0.0]
        stop = Mock()
        stop.is_set.return_value = False

        def advance(seconds):
            clock[0] += seconds
            if clock[0] <= 2:
                self.path.write_bytes(self.path.read_bytes() + b'next chunk')

        stop.wait.side_effect = advance
        with patch('script_gphoto.time.monotonic', side_effect=lambda: clock[0]):
            wait_until_stable(self.path, stable_seconds=2, timeout=10, stop_event=stop)
        self.assertEqual(clock[0], 4)
        self.assertEqual(stop.wait.call_count, 4)

    def test_initial_scan_finds_existing_media_recursively(self):
        nested = self.path.parent / 'nested'
        nested.mkdir()
        video = nested / 'VIDEO.MP4'
        video.write_bytes(b'video')
        mkv = nested / 'clip.MKV'
        mkv.write_bytes(b'video')
        (nested / 'copy.tmp').write_bytes(b'in progress')
        self.assertEqual(set(discover_files(self.path.parent)), {self.path, video, mkv})

    def test_hidden_and_system_files_are_skipped(self):
        root = self.path.parent
        hidden = [root / '._photo.jpg', root / '@eaDir' / 'photo.jpg' / 'thumb.jpg',
                  root / '#recycle' / 'old.jpg', root / '.hidden' / 'a.jpg']
        for path in hidden:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'junk')
        self.assertEqual(set(discover_files(root)), {self.path})
        submit = Mock()
        handler = PhotoHandler(submit, root)
        for path in hidden:
            handler.dispatch(FileCreatedEvent(str(path)))
        submit.assert_not_called()

    def test_symlinked_hidden_folder_is_not_submitted(self):
        root = self.path.parent
        visible = root / 'visible'
        visible.mkdir()
        (visible / 'photo.jpg').write_bytes(b'photo')
        (root / '.hidden').mkdir()
        try:
            (root / '.hidden' / 'link').symlink_to(visible, target_is_directory=True)
        except OSError:
            self.skipTest('Symbolic links are not available')
        submit = Mock()
        PhotoHandler(submit, root).dispatch(FileCreatedEvent(str(root / '.hidden' / 'link' / 'photo.jpg')))
        submit.assert_not_called()

    @unittest.skipIf(os.name != 'posix' or os.geteuid() == 0, 'Needs POSIX permissions as non-root')
    def test_unreadable_subfolder_does_not_stop_the_scan(self):
        root = self.path.parent
        locked = root / 'locked'
        locked.mkdir()
        (locked / 'secret.jpg').write_bytes(b'photo')
        later = root / 'zz-later'
        later.mkdir()
        (later / 'later.jpg').write_bytes(b'photo')
        listable = root / 'listable'
        listable.mkdir()
        (listable / 'hidden-metadata.jpg').write_bytes(b'photo')
        locked.chmod(0)
        listable.chmod(0o400)  # names can be listed, metadata cannot be read
        self.addCleanup(locked.chmod, 0o700)
        self.addCleanup(listable.chmod, 0o700)
        self.assertEqual(set(discover_files(root)), {self.path, later / 'later.jpg'})
        with self.assertRaises(FileNotFoundError):
            list(discover_files(root / 'missing'))

    def test_already_quiet_file_skips_the_stability_wait(self):
        clock = [100.0]
        store = StateStore(':memory:')
        self.addCleanup(store.close)
        client = Mock(upload=Mock(return_value={self.path.as_posix(): 'key'}))
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        processor = FileProcessor(client, store, stop_event=stop, stable_seconds=5)
        with patch('script_gphoto.time.monotonic', side_effect=lambda: clock[0]):
            processor.submit(self.path)
            clock[0] += 6
            processor.process_once()
            stop.wait.assert_not_called()
            self.assertEqual(store.get(self.path)['status'], 'done')
            self.assertEqual(processor.first_seen, {})
            # A file changed after submission still waits the full window.
            self.path.write_bytes(b'changed photo, longer')
            processor.submit(self.path)
            self.path.write_bytes(b'changed again, much longer photo')
            clock[0] += 6
            processor.process_once()
        self.assertGreaterEqual(sum(call.args[0] for call in stop.wait.call_args_list), 5)

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

    def test_batch_progress_tracker_is_thread_safe(self):
        tracker = BatchProgressTracker(batch_size=4, progress_interval=0)
        barrier = Barrier(2)

        def emit_events(name, terminal):
            path = f'/data/{name}'
            tracker.handle_event({'phase': 'hashing', 'path': path, 'bytes_completed': 10, 'bytes_total': 100})
            tracker.handle_event({'phase': 'uploading', 'path': path, 'bytes_completed': 50, 'bytes_total': 100})
            barrier.wait(timeout=5)
            tracker.handle_event({'phase': 'uploading', 'path': path, 'bytes_completed': 100, 'bytes_total': 100})
            tracker.handle_event({'phase': terminal, 'path': path, 'bytes_completed': 100, 'bytes_total': 100})

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(emit_events, 'a.jpg', 'complete')
            f2 = pool.submit(emit_events, 'b.jpg', 'skipped')
            f1.result()
            f2.result()

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot['active'], 0)
        self.assertEqual(snapshot['max_active'], 2)
        self.assertEqual(tracker.terminal_phase('/data/a.jpg'), 'complete')
        self.assertEqual(tracker.terminal_phase('/data/b.jpg'), 'skipped')

    def test_wait_until_stable_batch_cancels_on_shutdown(self):
        stop = Event()
        stop.set()
        job = {'path': str(self.path)}
        with self.assertRaises(InterruptedError):
            wait_until_stable_batch([(job, self.path, file_signature(self.path))], stop_event=stop)

    def test_wait_until_stable_batch_defers_changing_file_without_stalling_ready_files(self):
        other = self.path.with_name('other.jpg')
        other.write_bytes(b'other photo')
        clock = [0.0]
        stop = Mock()
        stop.is_set.return_value = False

        def advance(seconds):
            clock[0] += seconds
            if clock[0] == 1.0:
                other.write_bytes(other.read_bytes() + b'more bytes')

        stop.wait.side_effect = advance
        candidates = [
            ({'path': str(self.path)}, self.path, file_signature(self.path)),
            ({'path': str(other)}, other, file_signature(other)),
        ]
        with patch('script_gphoto.time.monotonic', side_effect=lambda: clock[0]):
            ready, changed_deferred, failed = wait_until_stable_batch(
                candidates, stable_seconds=2, stability_timeout=10, stop_event=stop,
            )
        self.assertEqual([path for _, path, _ in ready], [self.path])
        self.assertEqual([path for _, path, _ in changed_deferred], [other])
        self.assertEqual(failed, [])

    def test_processor_rejects_invalid_batch_or_thread_settings(self):
        store = StateStore(':memory:')
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            FileProcessor(Mock(), store, upload_threads=0)
        with self.assertRaises(ValueError):
            FileProcessor(Mock(), store, upload_batch_size=0)


if __name__ == '__main__':
    unittest.main()
