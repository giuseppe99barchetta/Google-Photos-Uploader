import json
import logging
import os
import signal
import stat
import sys
import time
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, quote_plus

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver as Observer

from upload_state import StateStore
from uploader_config import Config

MEDIA_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.heic', '.mp4', '.mov')
logger = logging.getLogger('gphotos_uploader')


class RedactingFormatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self, auth_data):
        super().__init__('%(asctime)sZ %(levelname)s %(threadName)s %(message)s',
                         datefmt='%Y-%m-%dT%H:%M:%S')
        tokens = parse_qs(auth_data).get('Token', [])
        self.secrets = sorted({value for token in [auth_data, *tokens]
                               for value in (token, quote_plus(token)) if value}, key=len, reverse=True)

    def format(self, record):
        message = super().format(record)
        for secret in self.secrets:
            message = message.replace(secret, '[REDACTED]')
        return message


def configure_logging(config):
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(config.auth_data))
    logging.basicConfig(level=config.log_level, handlers=[handler], force=True)


def install_signal_handlers(stop_event):
    def request_stop(signum, frame):
        stop_event.set()

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, request_stop)
    return previous


class ShutdownTimeout(RuntimeError):
    pass


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
    started = time.monotonic()
    result = client.upload(target=str(path), show_progress=False)
    media_key = result.get(path.as_posix()) if isinstance(result, dict) else None
    if not media_key:
        raise RuntimeError(f'Upload not confirmed: {path}')
    if file_signature(path) != signature:
        raise FileChangedError(f'File changed during upload; original retained: {path}')
    if on_confirmed:
        on_confirmed(signature, media_key)
    logger.info('Upload confirmed file=%s duration=%.2fs', path, time.monotonic() - started)
    if delete_after_upload:
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError('Cleanup deferred until restart')
        if file_signature(path) != signature:
            raise FileChangedError(f'File changed before cleanup: {path}')
        path.unlink()
        logger.info('Original removed file=%s', path)
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
            logger.info('Processing file=%s attempt=%s operation=%s', path, job['attempts'] + 1,
                        'cleanup' if job['media_key'] else 'upload')
            if not job['media_key']:
                def confirmed(signature, media_key):
                    nonlocal job
                    if not self.store.mark_uploaded(job, signature, media_key):
                        raise FileChangedError(f'File was replaced while queued: {path}')
                    job = {**job, 'signature': json.dumps(signature), 'media_key': media_key}

                upload_file(self.client, path, stop_event=self.stop_event,
                            on_confirmed=confirmed, **self.upload_options)
            if self.stop_event.is_set():
                return True  # Keep confirmation durable; resume cleanup after restart.
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
                    logger.info('Original removed file=%s', path)
            self.store.complete(path, job['signature'])
        except Exception as error:
            if isinstance(error, InterruptedError) and self.stop_event.is_set():
                return True
            # Upload confirmation must survive a cleanup failure.
            current = self.store.get(path)
            if current and current['signature'] == job['signature']:
                attempts, delay, exhausted = self.store.fail(
                    current, error, self.max_attempts, self.retry_base, self.retry_max)
                logger.warning('Original retained file=%s error=%s attempts=%s retry_in=%.1fs cooldown=%s',
                               path, type(error).__name__, attempts, delay, exhausted)
        return True

    def run(self):
        try:
            while not self.stop_event.is_set():
                if not self.process_once():
                    self.stop_event.wait(0.5)
        except Exception as error:
            logger.error('Worker stopped error=%s; pending work remains in SQLite', type(error).__name__)


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
        try:
            path = Path(path).absolute()
            if (path.suffix.lower() in MEDIA_EXTENSIONS and not path.is_symlink()
                    and path.resolve().is_relative_to(self.root)):
                self.submit(path)
        except (OSError, ValueError) as error:
            logger.warning('File event deferred to reconciliation error=%s', type(error).__name__)

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
        self.last_error = None

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
            self.last_error = None
            logger.info('Monitoring active folder=%s', self.root)
            return True
        except OSError as error:
            self.stop()
            if self.last_error != type(error).__name__:
                self.last_error = type(error).__name__
                logger.warning('Folder unavailable; reconnecting error=%s', self.last_error)
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


def run_service(config, client_factory, stop_event):
    watched_folder = config.root
    state_dir = config.state_dir
    write_health(state_dir, False)
    client = client_factory(auth_data=config.auth_data, timeout=config.request_timeout,
                            log_level=config.log_level)
    store = StateStore(state_dir / 'uploads.db')
    processor = FileProcessor(
        client, store, delete_after_upload=config.delete_after_upload,
        stop_event=stop_event,
        stable_seconds=config.stable_seconds, stability_timeout=config.stability_timeout,
        max_attempts=config.max_attempts, retry_base=config.retry_base, retry_max=config.retry_max)
    worker = Thread(target=processor.run, name='upload-worker', daemon=True)
    supervisor = WatchSupervisor(watched_folder, processor.submit, config.poll_interval)
    next_scan = 0
    try:
        worker.start()
        logger.info('Service started folder=%s delete_after_upload=%s',
                    watched_folder, config.delete_after_upload)
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
                    next_scan = time.monotonic() + config.scan_interval
                except OSError as error:
                    logger.warning('Reconciliation interrupted error=%s', type(error).__name__)
                    healthy = False
                    next_scan = 0
            write_health(state_dir, healthy and worker.is_alive())
            processor.stop_event.wait(5)
    finally:
        processor.stop_event.set()
        try:
            write_health(state_dir, False)
        except OSError:
            logger.error('Could not update health during shutdown')
        deadline = time.monotonic() + config.shutdown_timeout
        monitor_stop = Thread(target=supervisor.stop, name='monitor-shutdown', daemon=True)
        monitor_stop.start()
        monitor_stop.join(timeout=max(0, deadline - time.monotonic()))
        if worker.ident is not None:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        if worker.is_alive() or monitor_stop.is_alive():
            raise ShutdownTimeout('Shutdown deadline exceeded; pending work remains durable')
        store.close()
        logger.info('Service stopped; pending work saved')


def main():
    try:
        config = Config.from_env()
    except (ValueError, OSError) as error:
        logging.basicConfig(level=logging.ERROR)
        logger.error('Configuration error: %s', error)
        return 2
    configure_logging(config)
    # GPMC changes SIGINT when imported; install our handlers afterwards.
    from gpmc import Client

    stop_event = Event()
    previous = install_signal_handlers(stop_event)
    try:
        run_service(config, Client, stop_event)
        return 0
    except ShutdownTimeout:
        logger.error('Shutdown timed out; exiting with durable pending work')
        logging.shutdown()
        # GPMC's executor threads can otherwise keep the interpreter alive.
        os._exit(1)
    except Exception as error:
        logger.error('Service failed error=%s', type(error).__name__)
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    if sys.argv[1:] == ['--healthcheck']:
        sys.exit(0 if healthcheck(os.environ.get('STATE_DIR', '/state')) else 1)
    sys.exit(main())
