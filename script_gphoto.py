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


class PhotoHandler(FileSystemEventHandler):
    def __init__(self, client, delete_after_upload=False):
        self.client = client
        self.delete_after_upload = delete_after_upload

    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(MEDIA_EXTENSIONS):
            try:
                upload_file(self.client, event.src_path, self.delete_after_upload)
            except Exception as error:
                print(f'Upload failed; original retained: {event.src_path}: {error}')


def main():
    from gpmc import Client

    watched_folder = os.environ.get('WATCHED_FOLDER', '/data')
    client = Client(auth_data=os.environ.get('AUTH_DATA', ''))
    delete_after_upload = os.environ.get('DELETE_AFTER_UPLOAD', 'false').lower() == 'true'
    observer = Observer()
    observer.schedule(PhotoHandler(client, delete_after_upload), watched_folder, recursive=True)
    observer.start()
    print(f'Monitoring started on {watched_folder}...')
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == '__main__':
    main()
