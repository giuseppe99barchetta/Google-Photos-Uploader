import os
import time
from watchdog.observers.polling import PollingObserver as Observer
from watchdog.events import FileSystemEventHandler
from gpmc import Client

WATCHED_FOLDER = os.environ.get("WATCHED_FOLDER", "/data")
AUTH_DATA = os.environ.get("AUTH_DATA", "")

client = Client(auth_data=AUTH_DATA)

class PhotoHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(('.jpg', '.jpeg', '.png', '.heic', '.mp4')):
            print(f"Found new file: {event.src_path}")
            try:
                # File upload
                output = client.upload(target=event.src_path, show_progress=True)
                print(f"Uploaded: {output}")
                
                # Attempt to delete with 3 retries
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        os.remove(event.src_path)
                        print(f"File removed: {event.src_path}")
                        break
                    except PermissionError:
                        if attempt < max_retries - 1:
                            time.sleep(0.5 * (attempt + 1))
                            continue
                        raise
                        
            except Exception as e:
                print(f"Error: {e}")
                # Cleanup of leftover file in case of error
                if os.path.exists(event.src_path):
                    try:
                        os.remove(event.src_path)
                    except:
                        pass

if __name__ == "__main__":
    event_handler = PhotoHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCHED_FOLDER, recursive=True)
    observer.start()
    print(f"Monitoring started on {WATCHED_FOLDER}...")

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
