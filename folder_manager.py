"""Approved-root folder validation, safe directory browsing, filtering, and multi-folder watchers."""

import fnmatch
import logging
import os
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Event, RLock
from typing import Any

from script_gphoto import MEDIA_EXTENSIONS, SKIPPED_PREFIXES, WatchSupervisor

logger = logging.getLogger('gphotos_uploader')
ALLOWED_EXTENSIONS = frozenset(MEDIA_EXTENSIONS)
WINDOWS_DRIVE_RE = re.compile(r'^[a-zA-Z]:[\\/]')
SAFE_SUBFOLDER_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$')
IMMICH_EXCLUDE_DIRS = ('thumbs', 'encoded-video', 'profile', 'backups', '@eaDir')
IMMICH_EXCLUDE_PATTERNS = ('.*', '._*', '*.tmp', '*.part')


def normalize_extensions(extensions: Sequence[str] | None) -> list[str]:
    if extensions is None:
        return list(MEDIA_EXTENSIONS)
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in extensions:
        ext = str(raw).strip().lower()
        if ext and not ext.startswith('.'):
            ext = f'.{ext}'
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f'Unsupported media extension: {raw!r}. Supported: {", ".join(MEDIA_EXTENSIONS)}'
            )
        if ext not in seen:
            seen.add(ext)
            cleaned.append(ext)
    if not cleaned:
        raise ValueError('At least one supported media extension must be enabled')
    return cleaned


def normalize_string_list(values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = str(raw).strip()
        if not item:
            continue
        if '\x00' in item:
            raise ValueError('Filter pattern cannot contain null bytes')
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def validate_approved_path(
    candidate: str | Path,
    approved_roots: Sequence[Path],
    *,
    require_exists: bool = False,
) -> Path:
    if not approved_roots:
        raise PermissionError('No approved media roots are configured')
    raw = str(candidate).strip()
    if not raw or '\x00' in raw:
        raise ValueError('Folder path cannot be empty or contain null bytes')
    if os.name != 'nt' and (WINDOWS_DRIVE_RE.match(raw) or raw.startswith('\\\\')):
        raise PermissionError('Host Windows paths are not permitted; select a mounted path under an approved root')
    raw_path = Path(raw)
    if '..' in raw_path.parts:
        raise PermissionError('Path traversal segments (..) are not permitted')
    if not raw_path.is_absolute():
        raise PermissionError('Folder path must be an absolute path inside an approved root')

    canonical_roots = [root.resolve() for root in approved_roots]
    # Check symlinks along the path if it exists to prevent escape from approved roots.
    resolved = raw_path.resolve()
    if not any(resolved.is_relative_to(root) for root in canonical_roots):
        raise PermissionError('Selected path is outside approved media roots')
    if raw_path.is_symlink():
        raise PermissionError('Symlinked folders are not permitted')
    if require_exists:
        if not resolved.exists():
            raise ValueError('Selected folder does not exist')
        if not resolved.is_dir():
            raise ValueError('Selected path is not a directory')
        if not os.access(resolved, os.R_OK | os.X_OK):
            raise PermissionError('Selected folder is not readable')
    return resolved


def create_dedicated_subfolder(
    parent_root: str | Path,
    subfolder_name: str,
    approved_roots: Sequence[Path],
) -> Path:
    """Safely create (or reuse) a single child directory inside an approved writable root."""
    resolved_parent = validate_approved_path(parent_root, approved_roots, require_exists=True)
    cleaned = subfolder_name.strip()
    if not cleaned or not SAFE_SUBFOLDER_RE.match(cleaned) or cleaned in ('.', '..'):
        raise ValueError(
            'Subfolder name must be 1-64 characters using letters, numbers, dots, underscores, or hyphens'
        )
    candidate = resolved_parent / cleaned
    if candidate.is_symlink():
        raise PermissionError('Symlinked folders are not permitted')
    if not candidate.exists():
        if not os.access(resolved_parent, os.W_OK):
            raise PermissionError(
                'Selected root is read-only; mount a writable root or select an existing folder'
            )
        try:
            candidate.mkdir(parents=False, exist_ok=True)
        except OSError as error:
            raise PermissionError(
                f'Unable to create dedicated folder in {resolved_parent}: {type(error).__name__}'
            ) from None
    return validate_approved_path(candidate, approved_roots, require_exists=True)


def browse_approved_directories(
    path: str | None,
    approved_roots: Sequence[Path],
) -> dict[str, Any]:
    canonical_roots = [root.resolve() for root in approved_roots]
    roots_info = []
    for root in canonical_roots:
        exists = root.is_dir()
        readable = exists and os.access(root, os.R_OK | os.X_OK)
        writable = exists and os.access(root, os.W_OK)
        roots_info.append({
            'path': str(root),
            'name': root.name or str(root),
            'exists': exists,
            'readable': readable,
            'writable': writable,
            'is_root': True,
        })

    if not path or not path.strip():
        return {
            'current_path': None,
            'parent_path': None,
            'approved_roots': roots_info,
            'directories': roots_info,
        }

    resolved = validate_approved_path(path, canonical_roots, require_exists=True)
    matching_root = next(root for root in canonical_roots if resolved.is_relative_to(root))
    parent_path: str | None = None
    if resolved != matching_root and resolved.parent.is_relative_to(matching_root):
        parent_path = str(resolved.parent)

    directories = []
    try:
        entries = sorted(resolved.iterdir(), key=lambda item: item.name.lower())
    except OSError as error:
        raise PermissionError(f'Unable to list directory: {type(error).__name__}') from None

    for entry in entries:
        if entry.is_symlink():
            continue
        try:
            if not entry.is_dir():
                continue
            entry_resolved = entry.resolve()
            if not entry_resolved.is_relative_to(matching_root):
                continue
            readable = os.access(entry_resolved, os.R_OK | os.X_OK)
            writable = os.access(entry_resolved, os.W_OK)
            directories.append({
                'path': str(entry_resolved),
                'name': entry.name,
                'exists': True,
                'readable': readable,
                'writable': writable,
                'is_root': False,
            })
        except OSError:
            continue

    return {
        'current_path': str(resolved),
        'parent_path': parent_path,
        'approved_roots': roots_info,
        'directories': directories,
    }


def _is_dir_excluded(rel_parts: tuple[str, ...], exclude_dirs: Sequence[str]) -> bool:
    if not exclude_dirs or not rel_parts:
        return False
    lower_excludes = [item.strip('/\\').lower() for item in exclude_dirs if item.strip('/\\')]
    if not lower_excludes:
        return False
    lower_parts = [part.lower() for part in rel_parts]
    rel_posix = '/'.join(lower_parts)
    for excluded in lower_excludes:
        if '/' in excluded:
            if rel_posix == excluded or rel_posix.startswith(f'{excluded}/'):
                return True
        elif excluded in lower_parts:
            return True
    return False


def matches_folder_rules(path: Path, folder: Mapping[str, Any]) -> bool:
    folder_root = Path(folder['path']).resolve()
    try:
        if path.is_symlink():
            return False
        resolved = path.resolve()
        if not resolved.is_relative_to(folder_root):
            return False
        rel = resolved.relative_to(folder_root)
    except (OSError, ValueError):
        return False

    if not folder.get('recursive', True) and len(rel.parts) > 1:
        return False

    if any(part.startswith(SKIPPED_PREFIXES) for part in rel.parts):
        return False

    extensions = {ext.lower() for ext in folder.get('extensions', MEDIA_EXTENSIONS)} & ALLOWED_EXTENSIONS
    if resolved.suffix.lower() not in extensions:
        return False

    if _is_dir_excluded(rel.parts[:-1], folder.get('exclude_dirs') or ()):
        return False

    filename_lower = resolved.name.lower()
    for pattern in folder.get('exclude_patterns') or ():
        if fnmatch.fnmatch(filename_lower, str(pattern).lower()):
            return False

    try:
        info = resolved.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    min_size = int(folder.get('min_file_size') or 0)
    max_size = folder.get('max_file_size')
    if info.st_size < min_size:
        return False
    if max_size is not None and info.st_size > int(max_size):
        return False
    return True


def discover_folder_files(folder: Mapping[str, Any]) -> list[Path]:
    folder_root = Path(folder['path']).resolve()
    if not folder_root.is_dir():
        raise FileNotFoundError(f'Monitored folder unavailable: {folder_root}')
    recursive = bool(folder.get('recursive', True))
    exclude_dirs = folder.get('exclude_dirs') or ()
    extensions = {ext.lower() for ext in folder.get('extensions', MEDIA_EXTENSIONS)} & ALLOWED_EXTENSIONS
    exclude_patterns = [str(p).lower() for p in (folder.get('exclude_patterns') or ())]
    min_size = int(folder.get('min_file_size') or 0)
    max_size = folder.get('max_file_size')

    found: list[Path] = []
    if not recursive:
        for entry in folder_root.iterdir():
            if entry.is_symlink() or entry.name.startswith(SKIPPED_PREFIXES):
                continue
            if entry.suffix.lower() not in extensions:
                continue
            name_lower = entry.name.lower()
            if any(fnmatch.fnmatch(name_lower, pat) for pat in exclude_patterns):
                continue
            try:
                info = entry.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size < min_size:
                continue
            if max_size is not None and info.st_size > int(max_size):
                continue
            found.append(entry.resolve())
        return found

    def scan_error(error: OSError) -> None:
        if error.filename is None or os.path.abspath(error.filename) == os.path.abspath(folder_root):
            raise error
        logger.warning('Unreadable folder skipped folder=%s error=%s', error.filename, type(error).__name__)

    for directory, directories, filenames in os.walk(folder_root, onerror=scan_error):
        current_dir = Path(directory)
        try:
            rel_dir_parts = current_dir.resolve().relative_to(folder_root).parts
        except ValueError:
            directories[:] = []
            continue
        try:
            filtered_dirs: list[str] = []
            for dirname in directories:
                if dirname.startswith(SKIPPED_PREFIXES):
                    continue
                child = current_dir / dirname
                if child.is_symlink():
                    continue
                if _is_dir_excluded((*rel_dir_parts, dirname), exclude_dirs):
                    continue
                filtered_dirs.append(dirname)
            directories[:] = filtered_dirs

            for filename in filenames:
                if filename.startswith(SKIPPED_PREFIXES):
                    continue
                if not filename.lower().endswith(tuple(extensions)):
                    continue
                name_lower = filename.lower()
                if any(fnmatch.fnmatch(name_lower, pat) for pat in exclude_patterns):
                    continue
                candidate = current_dir / filename
                if candidate.is_symlink():
                    continue
                try:
                    info = candidate.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_size < min_size:
                    continue
                if max_size is not None and info.st_size > int(max_size):
                    continue
                found.append(candidate.resolve())
        except OSError as error:
            if os.path.abspath(directory) == os.path.abspath(folder_root):
                raise
            logger.warning('Unreadable folder skipped folder=%s error=%s', directory, type(error).__name__)
            directories[:] = []
            continue
    return found


class FolderWatcherPool:
    """Manages per-folder WatchSupervisor instances and reconciliation scans."""

    def __init__(
        self,
        submit_callback: Callable[[Path, int], bool],
        poll_interval: float = 1.0,
        on_detected: Callable[[int, Path], None] | None = None,
    ) -> None:
        self.submit_callback = submit_callback
        self.poll_interval = poll_interval
        self.on_detected = on_detected
        self._lock = RLock()
        self._supervisors: dict[int, tuple[WatchSupervisor, tuple[str, bool]]] = {}
        self._generations: dict[int, int] = {}

    def _make_submit(self, folder: Mapping[str, Any]) -> Callable[[Path], None]:
        folder_id = int(folder['id'])
        folder_snapshot = dict(folder)

        def handle_path(path: Path) -> None:
            resolved = Path(path).absolute()
            if not matches_folder_rules(resolved, folder_snapshot):
                return
            if self.submit_callback(resolved, folder_id) and self.on_detected:
                self.on_detected(folder_id, resolved)

        return handle_path

    def sync_watchers(self, folders: Sequence[Mapping[str, Any]]) -> list[int]:
        """Ensure watchers match active folders; returns folder IDs needing a fresh scan."""
        needs_scan: list[int] = []
        active_by_id = {
            int(folder['id']): folder
            for folder in folders
            if folder.get('enabled') and not folder.get('paused')
        }
        with self._lock:
            for existing_id in list(self._supervisors):
                if existing_id not in active_by_id:
                    supervisor, _ = self._supervisors.pop(existing_id)
                    self._generations.pop(existing_id, None)
                    supervisor.stop()
            for folder_id, folder in active_by_id.items():
                key = (str(folder['path']), bool(folder.get('recursive', True)))
                current = self._supervisors.get(folder_id)
                if current is not None and current[1] == key:
                    # Refresh callback rules if extensions/excludes changed.
                    current[0].submit = self._make_submit(folder)
                    continue
                if current is not None:
                    current[0].stop()
                supervisor = WatchSupervisor(
                    folder['path'],
                    self._make_submit(folder),
                    poll_interval=self.poll_interval,
                    recursive=bool(folder.get('recursive', True)),
                )
                self._supervisors[folder_id] = (supervisor, key)
                self._generations[folder_id] = supervisor.generation
                needs_scan.append(folder_id)
        return needs_scan

    def check_all(self, folders: Sequence[Mapping[str, Any]]) -> tuple[dict[int, str], list[int]]:
        """Check watcher health per folder. Returns (health_by_id, folders_needing_rescan)."""
        health_by_id: dict[int, str] = {}
        needs_scan = self.sync_watchers(folders)
        with self._lock:
            for folder in folders:
                folder_id = int(folder['id'])
                if not folder.get('enabled'):
                    health_by_id[folder_id] = 'disabled'
                    continue
                if folder.get('paused'):
                    health_by_id[folder_id] = 'paused'
                    continue
                entry = self._supervisors.get(folder_id)
                if entry is None:
                    health_by_id[folder_id] = 'unavailable'
                    continue
                supervisor, _ = entry
                prev_gen = self._generations.get(folder_id, 0)
                ok = supervisor.check()
                if supervisor.generation != prev_gen:
                    self._generations[folder_id] = supervisor.generation
                    if folder_id not in needs_scan:
                        needs_scan.append(folder_id)
                health_by_id[folder_id] = 'healthy' if ok else 'unavailable'
        return health_by_id, needs_scan

    def watcher_summary(self) -> dict[str, Any]:
        with self._lock:
            active_count = 0
            total = len(self._supervisors)
            for supervisor, _ in self._supervisors.values():
                if supervisor.observer is not None and supervisor.observer.is_alive():
                    active_count += 1
            return {
                'active_watchers': active_count,
                'configured_watchers': total,
                'healthy': total == 0 or active_count == total,
            }

    def scan_folder(
        self,
        folder: Mapping[str, Any],
        stop_event: Event | None = None,
    ) -> dict[str, Any]:
        folder_id = int(folder['id'])
        started = time.time()
        discovered = discover_folder_files(folder)
        newly_queued = 0
        for path in discovered:
            if stop_event is not None and stop_event.is_set():
                break
            if self.submit_callback(path, folder_id):
                newly_queued += 1
        return {
            'folder_id': folder_id,
            'discovered_count': len(discovered),
            'newly_queued': newly_queued,
            'scanned_at': started,
        }

    def stop_all(self) -> None:
        with self._lock:
            for supervisor, _ in self._supervisors.values():
                supervisor.stop()
            self._supervisors.clear()
            self._generations.clear()
