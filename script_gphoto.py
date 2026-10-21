import os
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver as Observer


MEDIA_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.heic', '.mp4', '.mov')


def upload_file(client, path, delete_after_upload=False):
    path = Path(path).absolute()
    result = client.upload(target=str(path), show_progress=False)
    media_key = result.get(path.as_posix()) if isinstance(result, dict) else None
    if not media_key:
        raise RuntimeError(f'Upload not confirmed: {path}')
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
