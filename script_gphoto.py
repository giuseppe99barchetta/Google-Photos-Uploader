import os
import stat
import time
from pathlib import Path
from threading import Event

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver as Observer


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
                stability_timeout=300, stop_event=None):
    path = Path(path).absolute()
    signature = wait_until_stable(path, stable_seconds, stability_timeout, stop_event)
    result = client.upload(target=str(path), show_progress=False)
    media_key = result.get(path.as_posix()) if isinstance(result, dict) else None
    if not media_key:
        raise RuntimeError(f'Upload not confirmed: {path}')
    if file_signature(path) != signature:
        raise FileChangedError(f'File changed during upload; original retained: {path}')
    print(f'Upload confirmed: {path}')
    if delete_after_upload:
        path.unlink()
        print(f'File removed: {path}')
    return media_key


class FileProcessor:
    def __init__(self, client, **upload_options):
        self.client = client
        self.upload_options = upload_options
        self.completed = {}

    def process(self, path):
        path = Path(path).absolute()
        try:
            signature = file_signature(path)
            if self.completed.get(str(path)) == signature:
                return
            upload_file(self.client, path, **self.upload_options)
            self.completed[str(path)] = signature
        except Exception as error:
            print(f'Upload failed; original retained: {path}: {error}')


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


def main():
    from gpmc import Client

    watched_folder = os.environ.get('WATCHED_FOLDER', '/data')
    client = Client(auth_data=os.environ.get('AUTH_DATA', ''))
    delete_after_upload = os.environ.get('DELETE_AFTER_UPLOAD', 'false').lower() == 'true'
    processor = FileProcessor(client, delete_after_upload=delete_after_upload)
    observer = Observer()
    observer.schedule(PhotoHandler(processor.process, watched_folder), watched_folder, recursive=True)
    observer.start()
    print(f'Monitoring started on {watched_folder}...')
    try:
        while True:
            for path in discover_files(watched_folder):
                processor.process(path)
            time.sleep(60)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == '__main__':
    main()
