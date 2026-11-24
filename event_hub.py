"""Thread-safe live upload state tracker and SSE event broadcaster."""

import asyncio
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Lock
from typing import Any

from script_gphoto import ACTIVE_UPLOAD_PHASES, TERMINAL_UPLOAD_PHASES


class EventHub:
    """Bridges synchronous uploader progress events to async SSE subscribers."""

    def __init__(self, progress_emit_interval: float = 0.25) -> None:
        self._lock = Lock()
        self._progress_emit_interval = progress_emit_interval
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = set()
        self._active: dict[str, dict[str, Any]] = {}
        self._batch_items: dict[str, dict[str, Any]] = {}
        self._batch_meta: dict[str, Any] = {
            'in_progress': False,
            'size': 0,
            'threads': 0,
            'started_at': None,
        }
        self._last_emit: dict[str, float] = {}

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        with self._lock:
            self._subscribers.add((loop, queue))
        return queue

    def unsubscribe(
        self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        with self._lock:
            self._subscribers.discard((loop, queue))

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        message = {'type': event_type, 'timestamp': time.time(), 'data': payload}
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            if loop.is_closed():
                self.unsubscribe(loop, queue)
                continue
            try:
                loop.call_soon_threadsafe(self._enqueue_nonblocking, queue, message)
            except RuntimeError:
                self.unsubscribe(loop, queue)

    @staticmethod
    def _enqueue_nonblocking(queue: asyncio.Queue[dict[str, Any]], message: dict[str, Any]) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass

    def start_batch(
        self,
        items: Sequence[tuple[dict[str, Any], Path, tuple[int, ...]]],
        threads: int,
        folder_names: Mapping[int, str] | None = None,
        account_names: Mapping[int, str] | None = None,
    ) -> None:
        now = time.time()
        folder_map = folder_names or {}
        account_map = account_names or {}
        batch_account_id = items[0][0].get('account_id') if items else None
        with self._lock:
            self._active.clear()
            self._batch_items.clear()
            self._last_emit.clear()
            self._batch_meta = {
                'in_progress': True,
                'size': len(items),
                'threads': threads,
                'account_id': batch_account_id,
                'account_name': account_map.get(batch_account_id) if batch_account_id is not None else None,
                'started_at': now,
            }
            for job, path, sig in items:
                posix = path.as_posix()
                fid = job.get('folder_id')
                aid = job.get('account_id')
                self._batch_items[posix] = {
                    'path': str(path),
                    'posix_path': posix,
                    'filename': path.name,
                    'folder_id': fid,
                    'folder_name': folder_map.get(fid) if fid is not None else None,
                    'account_id': aid,
                    'account_name': account_map.get(aid) if aid is not None else None,
                    'phase': 'queued',
                    'bytes_completed': 0,
                    'bytes_total': int(sig[2]),
                    'percent': 0.0 if sig[2] > 0 else None,
                    'speed_bps': None,
                    'eta_seconds': None,
                    'started_at': now,
                    'upload_started_at': None,
                    'updated_at': now,
                }
        self.publish('batch_started', self.live_snapshot())

    def handle_progress(self, event: Mapping[str, Any]) -> None:
        phase = str(event.get('phase') or '')
        if not phase:
            return
        posix = str(event.get('path') or '')
        if not posix:
            return
        try:
            bytes_completed = max(0, int(event.get('bytes_completed') or 0))
        except (TypeError, ValueError):
            bytes_completed = 0
        try:
            bytes_total = max(0, int(event.get('bytes_total') or 0))
        except (TypeError, ValueError):
            bytes_total = 0

        now = time.time()
        mono = time.monotonic()
        should_emit = False
        with self._lock:
            item = self._batch_items.get(posix)
            if item is None:
                filename = str(event.get('filename') or Path(posix).name)
                item = {
                    'path': posix,
                    'posix_path': posix,
                    'filename': filename,
                    'folder_id': None,
                    'folder_name': None,
                    'phase': phase,
                    'bytes_completed': 0,
                    'bytes_total': bytes_total,
                    'percent': None,
                    'speed_bps': None,
                    'eta_seconds': None,
                    'started_at': now,
                    'upload_started_at': None,
                    'updated_at': now,
                }
                self._batch_items[posix] = item

            prev_phase = item['phase']
            if bytes_total > 0:
                item['bytes_total'] = bytes_total
            total = item['bytes_total']
            item['bytes_completed'] = min(bytes_completed, total) if total > 0 else bytes_completed
            item['phase'] = phase
            item['updated_at'] = now

            if phase == 'uploading':
                if item['upload_started_at'] is None:
                    item['upload_started_at'] = mono
                elapsed = mono - item['upload_started_at']
                if elapsed >= 0.25 and item['bytes_completed'] > 0:
                    speed = item['bytes_completed'] / elapsed
                    item['speed_bps'] = round(speed, 1)
                    if total > item['bytes_completed'] and speed > 0:
                        item['eta_seconds'] = round((total - item['bytes_completed']) / speed, 1)
                    else:
                        item['eta_seconds'] = 0.0 if total > 0 and item['bytes_completed'] >= total else None
                else:
                    item['speed_bps'] = None
                    item['eta_seconds'] = None
            elif phase in ('complete', 'skipped'):
                if total > 0:
                    item['bytes_completed'] = total
                item['eta_seconds'] = 0.0 if total > 0 else None
                item['speed_bps'] = None
            else:
                item['speed_bps'] = None
                item['eta_seconds'] = None

            item['percent'] = (
                round(min(100.0, (item['bytes_completed'] * 100.0) / total), 1)
                if total > 0
                else None
            )

            if phase in ACTIVE_UPLOAD_PHASES:
                self._active[posix] = dict(item)
            elif phase in TERMINAL_UPLOAD_PHASES:
                self._active.pop(posix, None)

            last = self._last_emit.get(posix, 0.0)
            if phase != prev_phase or mono - last >= self._progress_emit_interval:
                self._last_emit[posix] = mono
                should_emit = True

        if should_emit:
            self.publish('upload_progress', self.live_snapshot())

    def finish_batch(self, summary: dict[str, Any]) -> None:
        with self._lock:
            self._active.clear()
            self._batch_meta['in_progress'] = False
            self._batch_meta['last_summary'] = dict(summary)
        self.publish('batch_completed', {**self.live_snapshot(), 'summary': summary})

    def live_snapshot(self) -> dict[str, Any]:
        with self._lock:
            active_list = [
                {k: v for k, v in item.items() if k != 'upload_started_at'}
                for item in self._active.values()
            ]
            batch_items = [
                {k: v for k, v in item.items() if k != 'upload_started_at'}
                for item in self._batch_items.values()
            ]
            total_speed = sum(
                item['speed_bps'] for item in active_list if item.get('speed_bps') is not None
            )
            has_speed = any(item.get('speed_bps') is not None for item in active_list)
            return {
                'active_count': len(active_list),
                'active_uploads': active_list,
                'batch_items': batch_items,
                'batch': dict(self._batch_meta),
                'aggregate_speed_bps': round(total_speed, 1) if has_speed else None,
            }
