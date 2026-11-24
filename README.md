# Google Photos Uploader

![Google Photos Uploader: automatically upload photos from a local folder or NAS to Google Photos.](media/readme-hero.png)

Monitor a folder and upload photos and videos to Google Photos using
[GPMC](https://github.com/xob0t/gpmc) without consuming storage space. Supports local folders and host-mounted SMB/NAS shares, with Docker images for AMD64 and ARM64.

GPMC uses a reverse-engineered mobile API. Storage accounting and authentication
are controlled by Google and may change; verify the results in your own account.

## File handling

- **Originals are kept by default**, including after failed or unconfirmed uploads.
- JPG, JPEG, PNG, HEIC, MP4, MOV and MKV are supported, including uppercase extensions.
- Existing files are scanned at startup and every `SCAN_INTERVAL`. New files,
  modifications and renames are also detected by polling every `POLL_INTERVAL`.
  Symbolic links are excluded.
- Hidden and system names are skipped: anything whose file or folder name starts
  with `.`, `@` or `#` (macOS `._*` files, dot-folders, Synology `@eaDir` and
  `#recycle`). Rename user folders that start with these characters.
- A subfolder that cannot be read is logged and skipped; the rest of the folder is
  still processed. Only an unreadable watched folder marks the service unhealthy.
- Nonempty files must remain unchanged for `STABLE_SECONDS` before uploading,
  counted from when the service first saw them, so an existing backlog is not
  delayed file by file.
- An upload is confirmed only when GPMC returns a media key for that exact path.
  The file's identity, size and modification time are checked again after upload.
- SQLite records pending work and confirmed uploads. Unchanged completed files
  are skipped after restart. GPMC also checks remote duplicates by content hash.
- Errors use exponential backoff. After `MAX_ATTEMPTS`, the file stays on disk;
  reconciliation can reactivate it after the `RETRY_MAX` cooldown.

**Optional deletion:** setting `DELETE_AFTER_UPLOAD=true` permits removal only
following confirmation and a check that the source has not changed. Confirmation
is committed before removal, so a failed removal can be retried after restart
without repeating the upload. Use a dedicated upload folder and keep backups.

Changing the Google account, watched folder or deletion mode resets the local
queue's scope and rescans the source. Files are confirmed remotely again before
they can be removed under a newly enabled deletion setting. Refreshing credentials
for the same account preserves the queue. Remote hash checks avoid duplicate uploads.

File stability is a heuristic: a copy paused longer than the configured interval
can appear complete. For reliable ingestion, copy to an unsupported temporary
extension such as `.tmp` in the same directory, close the file, then rename it to
its final `.jpg` or `.mp4` name. Avoid modifying files while they are being uploaded.

## Getting started

Requirements: Docker with Compose, a readable photo folder and authentication
data obtained below. Mount SMB/NAS shares **on the host** before starting Compose.

1. Copy `.env.example` to `.env` and fill in your credentials and folder:

   ```dotenv
   AUTH_DATA='androidId=...&Email=...&Token=...'
   PHOTO_FOLDER=./photos
   DELETE_AFTER_UPLOAD=false
   ```

   The authentication string must contain the complete captured request; the
   example above is abbreviated. `.env`, `secrets/` and `photos/` are ignored by Git.
   The Docker build context includes only the required application files.

2. Create the photo folder or verify that your host-mounted NAS folder exists.
   Compose deliberately does not create a missing source directory.

3. Start and inspect the service:

   ```bash
   docker compose up -d
   docker compose logs -f
   docker compose ps
   ```

4. Stop it with `docker compose down`. The named `uploader-state` volume persists
   the queue, confirmations, health status and GPMC cache. Keep it on local
   storage; SQLite should not be placed on an SMB/NFS share. `down -v` deletes it.

The container runs as UID/GID 1000. The photo directory needs read/search
permissions, plus write permission if deletion is enabled. The state volume must
always be writable. If you override the runtime UID/GID, adjust the state volume's
ownership as well. One service instance should own each state volume.

## Configuration

Compose passes these options from `.env` or the host environment. Direct Python
runs read the same environment variables. Invalid values fail before workers start.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTH_DATA` | empty | Complete authentication request; required unless using a file |
| `AUTH_DATA_FILE` | empty | Container path to an authentication file; mutually exclusive with `AUTH_DATA` |
| `PHOTO_FOLDER` | `./photos` | Host bind-mount source, used by Compose |
| `DELETE_AFTER_UPLOAD` | `false` | Explicitly enable source deletion; accepts `true` or `false` |
| `STABLE_SECONDS` | `5` | Time a nonempty file must remain unchanged |
| `STABILITY_TIMEOUT` | `300` | Maximum wait for a stable file per attempt |
| `SCAN_INTERVAL` | `60` | Interval between reconciliation scans |
| `POLL_INTERVAL` | `30` | Watchdog polling interval; each poll reads every file's metadata |
| `REQUEST_TIMEOUT` | `60` | Per-request timeout passed to GPMC; its internal retries can extend an operation |
| `UPLOAD_THREADS` | `2` | Concurrent upload threads per GPMC batch (`1`–`8`) |
| `UPLOAD_BATCH_SIZE` | `4` | Maximum pending files collected per GPMC batch (`1`–`64`) |
| `APPROVED_MEDIA_ROOTS` | `/data` | Comma-separated container mount roots exposed to the dashboard folder browser |
| `UPLOAD_QUALITY` | `original` | `original` or `saver` (Storage Saver) |
| `USE_QUOTA` | `false` | Whether to count uploads against Google account storage quota |
| `DUPLICATE_DETECTION` | `true` | Enable GPMC SHA-1 hash duplicate lookup |
| `FILENAME_CONFLICT_POLICY` | `hash_dedup` | `hash_dedup` or `skip_existing_filename` |
| `AUTO_RETRY_FAILED` | `true` | Automatically reactivate failed jobs after cooldown during reconciliation |
| `DASHBOARD_ENABLED` | `true` (Compose) / `false` (CLI) | Serve the FastAPI + React web dashboard in the same process |
| `DASHBOARD_BIND_HOST` | `127.0.0.1` | Host interface bound by Compose (`0.0.0.0` for trusted LAN access) |
| `DASHBOARD_PORT` | `8080` | Host port mapped to the dashboard |
| `DASHBOARD_USERNAME` | `admin` | Local dashboard administrator username |
| `DASHBOARD_PASSWORD` | empty | Optional initial password (>= 8 chars); if blank, first-visit setup prompts for one |
| `DASHBOARD_SESSION_SECRET` | empty | Optional hex signing secret for session cookies (auto-generated in `/state` if blank) |
| `DASHBOARD_SECURE_COOKIES` | `false` | Set `true` when serving behind an HTTPS reverse proxy |
| `MAX_ATTEMPTS` | `5` | Failed attempts before cooldown |
| `RETRY_BASE` | `5` | Initial retry delay |
| `RETRY_MAX` | `3600` | Maximum retry delay and cooldown duration |
| `SHUTDOWN_TIMEOUT` | `120` | Grace period for in-flight work before forced process exit |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR or CRITICAL |

All durations are in seconds and must be positive. `MAX_ATTEMPTS`,
`REQUEST_TIMEOUT`, `UPLOAD_THREADS` (`1`–`8`), and `UPLOAD_BATCH_SIZE` (`1`–`64`)
must be integers. `STABILITY_TIMEOUT` must be at least `STABLE_SECONDS`;
`RETRY_MAX` must be at least `RETRY_BASE`. If you increase `SHUTDOWN_TIMEOUT`,
also increase Compose's `stop_grace_period` (default: 150s).

`WATCHED_FOLDER` and `STATE_DIR` default to `/data` and `/state` in the container.
They must be separate, non-overlapping directories. Compose fixes them to match
its mounts. For direct Python runs, set both to appropriate local paths.
`GP_AUTH_DATA` is also accepted for direct runs as a fallback to `AUTH_DATA`.

### Authentication from a mounted secret

To keep credentials out of the container environment, leave `AUTH_DATA` empty in
`.env`, save the full request in `secrets/auth_data.txt`, and add a
`compose.override.yaml` containing:

```yaml
services:
  gphotos-uploader:
    environment:
      AUTH_DATA: ""
      AUTH_DATA_FILE: /run/secrets/gphotos_auth
    secrets:
      - gphotos_auth
secrets:
  gphotos_auth:
    file: ./secrets/auth_data.txt
```

Ensure the runtime user can read the file. Keep this file private. Known input
tokens are redacted in application logs; avoid sharing credentials or debug logs.

Load the secret override explicitly with both files:

```bash
docker compose -f docker-compose.yml -f compose.override.yaml up -d
```

Use the same `-f` options for subsequent Compose commands. See the
[Docker documentation on merging Compose files](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/).

## Health and recovery

A heartbeat checks that the worker and Watchdog emitter are alive and the watched
folder can be accessed. Missing folders and stopped observers are retried; a
reconnected observer triggers reconciliation. Heartbeats older than 30 seconds
are unhealthy. Docker checks every 15 seconds after its startup grace period.

```bash
docker compose exec gphotos-uploader python /app/script_gphoto.py --healthcheck
```

An unhealthy status alone does not trigger a restart under Docker Compose. The
service attempts reconnection itself; fatal worker failures exit so the restart
policy can take effect. A filesystem call stuck in the kernel may need host
intervention. A readable empty host mountpoint does not prove the NAS is mounted.
Health also does not prove Google authentication or a previous upload succeeded;
check upload confirmation and retry messages in the logs.

SIGINT and SIGTERM request an orderly stop. Work and confirmations remain in
SQLite. An in-flight GPMC operation may finish within `SHUTDOWN_TIMEOUT`; after
that the process exits and the next startup resumes pending work.

## Web Dashboard & Sync Manager

The container runs a single unified Python process combining the uploader worker,
folder watchers, FastAPI REST/SSE API, and React web dashboard (bound to
`http://localhost:8080` by default).

- **First visit:** if `DASHBOARD_PASSWORD` is unset, the dashboard prompts you
  once to create an administrator password (minimum 8 characters). Password
  hashes and session signing keys are stored with `0600` permissions in
  `/state/dashboard_auth.json` (never in SQLite).
- **Adding additional Windows folders safely:** mount extra host directories in
  `compose.override.yaml` (recommended read-only when `DELETE_AFTER_UPLOAD=false`)
  and include their container mount points in `APPROVED_MEDIA_ROOTS`:

  ```yaml
  services:
    gphotos-uploader:
      environment:
        APPROVED_MEDIA_ROOTS: /data,/media/pictures,/media/videos
      volumes:
        - type: bind
          source: C:\Users\you\Pictures
          target: /media/pictures
          read_only: true
          bind:
            create_host_path: false
        - type: bind
          source: C:\Users\you\Videos
          target: /media/videos
          read_only: true
          bind:
            create_host_path: false
  ```

  After restarting Compose, open **Monitored Folders -> Add Monitored Folder**
  and browse inside `/data`, `/media/pictures`, or `/media/videos`. Paths outside
  `APPROVED_MEDIA_ROOTS` and `..` traversal attempts are rejected server-side.

- **Bundled Immich stack & multi-user auto-sync (`docker-compose.immich.yml`):**
  1. Set a strong alphanumeric `IMMICH_DB_PASSWORD` and your desired
     `IMMICH_UPLOAD_LOCATION` (e.g. `C:\Users\you\immich-library`) in `.env`.
  2. Start Immich and Google Photos Sync Manager together with one command:
     ```cmd
     docker compose -f docker-compose.yml -f compose.override.yaml -f docker-compose.immich.yml up -d --build
     ```
     This starts Immich at `http://localhost:2283` (PostgreSQL with VectorChord,
     Valkey, Machine Learning, and Immich Server) and automatically mounts
     Immich's storage **read-only** into Sync Manager at `/media/immich`.
  3. Open Immich (`http://localhost:2283`), create your admin user (User A), and
     generate an Admin API key (**Account Settings → API Keys → New API Key**).
  4. Open Sync Manager (`http://localhost:8080`) → **Google Accounts** to connect
     Account A and Account B (each needs its own captured Android `AUTH_DATA`;
     both can share the same device `androidId`).
  5. Open **Immich Sync** in Sync Manager, paste your Immich Admin API key
     (saved with `0600` permissions in `/state/immich_api.secret`), create User B
     if needed, and click **Link & Auto-Sync** next to each Immich user to bind
     User A → Account A and User B → Account B. Immich's `thumbs`,
     `encoded-video`, `profile`, and `backups` directories are excluded
     automatically, and Sync Manager activates watching as soon as each user
     uploads their first photo from the Immich mobile app.

- **Updating Android authentication securely:**
  - *From the Dashboard:* open **Authentication**, paste fresh `AUTH_DATA`, click
    **Validate Credentials**, then **Validate & Save**. The backend validates
    against Google first (preserving working credentials if validation fails),
    writes atomically with `0600` permissions to `/state/auth_override.secret`
    (never modifying read-only `/run/secrets/gphotos_auth`), and reloads the
    client for the next batch.
  - *From the Windows host:* replace `secrets\auth_data.txt`, remove any
    `/state/auth_override.secret` override if present, and restart the container.

- **Backup and recovery:** schema upgrades automatically create a pre-migration
  SQLite online backup (`uploads.backup-v1-to-v2-*.db`) inside the
  `uploader-state` volume. You can also take an on-demand backup without stopping
  the service:

  ```cmd
  docker compose exec gphotos-uploader python -c "import sqlite3; s=sqlite3.connect('/state/uploads.db'); d=sqlite3.connect('/state/uploads-manual-backup.db'); s.backup(d); d.close(); s.close()"
  ```

## How to get your authentication data

Store the captured request securely. Authentication may need to be refreshed if
Google rejects or revokes it.

### ✅ Option 1 – ReVanced (No Root Required)

1. Install Google Photos ReVanced on your Android device:  
   - Install [GmsCore](https://github.com/ReVanced/GmsCore/releases)  
   - Install the patched APK from [here](https://github.com/j-hc/revanced-magisk-module/releases) or patch it yourself  
2. Connect your device to your PC via ADB.
3. Run the following in your terminal:

    **Windows:**
    ```cmd
    adb logcat | FINDSTR "auth%2Fphotos.native"
    ```

    **Linux/macOS:**
    ```bash
    adb logcat | grep "auth%2Fphotos.native"
    ```

4. If you've used ReVanced before, remove your Google Account from GmsCore.
5. Launch Google Photos ReVanced and log into your account.
6. You should see logs appear in your terminal.
7. Copy the full line starting from `androidId=` to the end. That’s your `AUTH_DATA`! 🎉

---

### 🛠 Option 2 – Official Google Photos App (Root Required)

<details>
  <summary><strong>Click to expand</strong></summary>

1. Use a rooted Android device or emulator (Android 9–13 recommended).
2. Connect your device to your PC via ADB.
3. Install [HTTP Toolkit](https://httptoolkit.com).
4. In HTTP Toolkit, go to **Intercept → Android Device via ADB**.
5. Apply the filter:

    ```text
    contains(https://www.googleapis.com/auth/photos.native)
    ```

    Or for older versions:

    ```text
    contains(www.googleapis.com%2Fauth%2Fplus.photos.readwrite)
    ```

6. Open Google Photos and log into your account.
7. Look for the captured request and copy the **request body** as plain text.  
   ![http_toolkit_tip](media/image.png)
8. That’s your `AUTH_DATA`! 🎉

</details>

---

## Building and updating

The default Compose file runs the published image. To apply local source changes:

```bash
docker build -t ghcr.io/giuseppe99barchetta/google-photos-uploader:latest .
docker compose up -d --force-recreate --pull never
docker compose logs -f
```

Run these commands from the repository root. A subsequent `docker compose pull`
replaces your local image with the published one. The build checks dependency
compatibility and imports as `appuser` before producing the image.

To update from the registry:

```bash
docker compose pull
docker compose up -d --force-recreate
```

Images are also tagged by commit SHA and release tag. Select an available
commit tag in Compose when you need predictable upgrades or a rollback.

## Development and checks

Python 3.11 or newer is required. Create a virtual environment, then run:

```bash
python -m pip install -r requirements-dev.txt
python -m pip check
python -m ruff check .
python -m unittest discover -s tests -v
```

Tests use temporary files and fake credentials; they do not contact Google.
They cover preservation, stability, reconciliation, persistent retries, cleanup
recovery, polling, health, logging and shutdown. CI runs on Windows and Linux
with Python 3.11 and 3.12, then runs the tests as the container runtime user.
Publishing on the default branch or version tags waits for these checks.

For a direct run, set `AUTH_DATA` or `AUTH_DATA_FILE`, `WATCHED_FOLDER`, and a
separate writable `STATE_DIR`, then run `python script_gphoto.py`.
