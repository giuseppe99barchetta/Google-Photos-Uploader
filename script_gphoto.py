import os
import json
import stat
import sys
import time
from pathlib import Path
from threading import Event, Thread

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver as Observer

from upload_state import StateStore


MEDIA_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.heic', '.mp4', '.mov')


class FileChangedError(RuntimeError):
    pass


def file_signature(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'Not a regular file: {path}')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def wait_until_stable(path, stable_seconds=5, timeout=300, stop_event=None):
    stop_event = stop_event or Event()
    deadline = time.monotonic() + timeout
    previous = None
    unchanged_since = time.monotonic()
    while not stop_event.is_set():
        signature = file_signature(path)
        now = time.monotonic()
        if signature != previous:
            previous, unchanged_since = signature, now
        if signature[2] > 0 and now - unchanged_since >= stable_seconds:
            return signature
        if now >= deadline:
            raise TimeoutError(f'File is empty or still changing: {path}')
        stop_event.wait(min(1, max(0.01, stable_seconds / 2), deadline - now))
    raise InterruptedError('Upload cancelled during shutdown')


def upload_file(client, path, delete_after_upload=False, stable_seconds=5,
                stability_timeout=300, stop_event=None, on_confirmed=None):
    path = Path(path).absolute()
    signature = wait_until_stable(path, stable_seconds, stability_timeout, stop_event)
    result = client.upload(target=str(path), show_progress=False)
    media_key = result.get(path.as_posix()) if isinstance(result, dict) else None
    if not media_key:
        raise RuntimeError(f'Upload not confirmed: {path}')
    if file_signature(path) != signature:
        raise FileChangedError(f'File changed during upload; original retained: {path}')
    if on_confirmed:
        on_confirmed(signature, media_key)
    print(f'Upload confirmed: {path}')
    if delete_after_upload:
        path.unlink()
        print(f'File removed: {path}')
    return media_key


class FileProcessor:
    def __init__(self, client, store, delete_after_upload=False, stop_event=None,
                 max_attempts=5, retry_base=5, retry_max=3600, **upload_options):
        self.client = client
        self.store = store
        self.delete_after_upload = delete_after_upload
        self.stop_event = stop_event or Event()
        self.max_attempts = max_attempts
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.upload_options = upload_options

    def submit(self, path):
        path = Path(path).absolute()
        try:
            self.store.enqueue(path, file_signature(path))
        except FileNotFoundError:
            pass  # A queued event may refer to a file already moved or removed.

    def process_once(self):
        job = self.store.next_job()
        if not job:
            return False
        path = Path(job['path'])
        try:
            if not job['media_key']:
                def confirmed(signature, media_key):
                    nonlocal job
                    if not self.store.mark_uploaded(job, signature, media_key):
                        raise FileChangedError(f'File was replaced while queued: {path}')
                    job = {**job, 'signature': json.dumps(signature), 'media_key': media_key}

                upload_file(self.client, path, stop_event=self.stop_event,
                            on_confirmed=confirmed, **self.upload_options)
            if self.delete_after_upload:
                try:
                    current_signature = file_signature(path)
                except FileNotFoundError:
                    current_signature = None
                if current_signature is not None:
                    if list(current_signature) != json.loads(job['signature']):
                        self.submit(path)
                        raise FileChangedError(f'File changed before cleanup: {path}')
                    path.unlink()
            self.store.complete(path, job['signature'])
        except InterruptedError:
            pass  # Leave the job pending for the next process.
        except Exception as error:
            print(f'Upload failed; original retained: {path}: {error}')
            # Upload confirmation must survive a cleanup failure.
            current = self.store.get(path)
            if current and current['signature'] == job['signature']:
                self.store.fail(current, error, self.max_attempts, self.retry_base, self.retry_max)
        return True

    def run(self):
        while not self.stop_event.is_set():
            if not self.process_once():
                self.stop_event.wait(0.5)


def discover_files(root):
    def scan_error(error):
        raise error

    for directory, directories, filenames in os.walk(root, onerror=scan_error):
        directories[:] = [name for name in directories
                          if not (Path(directory) / name).is_symlink()]
        for name in filenames:
            path = Path(directory) / name
            if path.suffix.lower() in MEDIA_EXTENSIONS and not path.is_symlink():
                yield path.absolute()


class PhotoHandler(FileSystemEventHandler):
    def __init__(self, submit, watched_folder):
        self.submit = submit
        self.root = Path(watched_folder).resolve()

    def submit_path(self, path):
        path = Path(path).absolute()
        if (path.suffix.lower() in MEDIA_EXTENSIONS and not path.is_symlink()
                and path.resolve().is_relative_to(self.root)):
            self.submit(path)

    def on_created(self, event):
        if not event.is_directory:
            self.submit_path(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.submit_path(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.submit_path(event.src_path)


class WatchSupervisor:
    def __init__(self, root, submit, poll_interval=1):
        self.root = Path(root)
        self.handler = PhotoHandler(submit, root)
        self.poll_interval = poll_interval
        self.observer = None
        self.identity = None
        self.generation = 0

    def stop(self):
        if self.observer is not None:
            self.observer.stop()
            if self.observer.ident is not None:
                self.observer.join()
            self.observer = None

    def check(self):
        try:
            info = self.root.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError(str(self.root))
            with os.scandir(self.root) as entries:
                next(entries, None)
            identity = (info.st_dev, info.st_ino)
            if (self.observer is not None and self.observer.is_alive()
                    and self.observer.emitters
                    and all(emitter.is_alive() for emitter in self.observer.emitters)
                    and identity == self.identity):
                return True
            self.stop()
            self.observer = Observer(timeout=self.poll_interval)
            self.observer.schedule(self.handler, str(self.root), recursive=True)
            self.observer.start()
            self.identity = identity
            self.generation += 1
            print(f'Monitoring active: {self.root}')
            return True
        except OSError as error:
            self.stop()
            print(f'Watched folder unavailable; waiting to reconnect: {type(error).__name__}')
            return False


def write_health(state_dir, healthy):
    path = Path(state_dir) / 'health.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'ok': healthy, 'updated_at': time.time()}), encoding='utf-8')
    temporary.replace(path)


def healthcheck(state_dir, max_age=30):
    try:
        data = json.loads((Path(state_dir) / 'health.json').read_text(encoding='utf-8'))
        age = time.time() - data['updated_at']
        return data['ok'] is True and 0 <= age <= max_age
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main():
    from gpmc import Client

    watched_folder = os.environ.get('WATCHED_FOLDER', '/data')
    client = Client(auth_data=os.environ.get('AUTH_DATA', ''))
    delete_after_upload = os.environ.get('DELETE_AFTER_UPLOAD', 'false').lower() == 'true'
    state_dir = Path(os.environ.get('STATE_DIR', '/tmp/gphotos-uploader'))
    state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(state_dir / 'uploads.db')
    processor = FileProcessor(client, store, delete_after_upload=delete_after_upload)
    worker = Thread(target=processor.run, name='upload-worker', daemon=True)
    worker.start()
    supervisor = WatchSupervisor(watched_folder, processor.submit)
    next_scan = 0
    try:
        while not processor.stop_event.is_set():
            if not worker.is_alive():
                raise RuntimeError('Upload worker stopped unexpectedly')
            generation = supervisor.generation
            healthy = supervisor.check()
            if supervisor.generation != generation:
                next_scan = 0
            if healthy and time.monotonic() >= next_scan:
                try:
                    heartbeat = time.monotonic()
                    for path in discover_files(watched_folder):
                        if processor.stop_event.is_set():
                            break
                        processor.submit(path)
                        if time.monotonic() - heartbeat >= 5:
                            write_health(state_dir, worker.is_alive())
                            heartbeat = time.monotonic()
                    next_scan = time.monotonic() + 60
                except OSError:
                    healthy = False
                    next_scan = 0
            write_health(state_dir, healthy and worker.is_alive())
            processor.stop_event.wait(5)
    except KeyboardInterrupt:
        pass
    finally:
        processor.stop_event.set()
        write_health(state_dir, False)
        supervisor.stop()
        worker.join()
        store.close()


if __name__ == '__main__':
    if sys.argv[1:] == ['--healthcheck']:
        sys.exit(0 if healthcheck(os.environ.get('STATE_DIR', '/tmp/gphotos-uploader')) else 1)
    main()
