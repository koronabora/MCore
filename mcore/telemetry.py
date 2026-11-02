"""Local read-only torrent observations, exported atomically for mcore-top."""
import asyncio
import json
import logging
import os
from pathlib import Path
import tempfile
import time

import libtorrent as lt

log = logging.getLogger(__name__)


def snapshot(torrents):
    rows = []
    for key, handle in list(torrents.handles.items()):
        try:
            # Do not call Torrents.status(): it updates last_used and piece deadlines.
            status = handle.status(lt.status_flags_t.query_accurate_download_counters)
            info = handle.torrent_file() if status.has_metadata else None
            total = info.total_size() if info else 0
            done = max(0, status.total_done)
            error = torrents.errors.get(key) or (status.errc.message() if status.errc.value() else '')
            metadata_only = key in torrents.metadata_only
            state = {0: 'В очереди проверки', 1: 'Проверка', 2: 'Получение метаданных',
                     3: 'Скачивание', 4: 'Выбранные файлы готовы', 5: 'Раздача',
                     6: 'Выделение места', 7: 'Проверка resume'}.get(int(status.state), 'Ожидание')
            if error:
                state = 'Ошибка'
            elif status.paused:
                state = 'Пауза'
            elif metadata_only and status.has_metadata:
                state = 'Только индекс'
            elif status.has_metadata and total and done >= total:
                state = 'Раздача'
            rows.append(dict(id=key, name=(info.name() if info else status.name) or key, state=state,
                             metadata_only=metadata_only, size=total, downloaded=done,
                             percent=min(100, 100*done/total) if total else None,
                             uploaded=max(0, status.all_time_upload),
                             download_rate=max(0, status.download_payload_rate),
                             upload_rate=max(0, status.upload_payload_rate),
                             peers=status.num_peers, seeds=status.num_seeds, error=str(error)))
        except (RuntimeError, ValueError):
            # A removed/invalid handle should not suppress other torrents.
            rows.append(dict(id=key, name=key, state='Недоступна', metadata_only=key in torrents.metadata_only,
                             size=None, downloaded=None, percent=None, uploaded=None,
                             download_rate=None, upload_rate=None, peers=None, seeds=None,
                             error='Не удалось прочитать состояние раздачи'))
    return dict(schema=1, pid=os.getpid(), updated=time.time(), torrents=rows)


def write_snapshot(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.torrent-stats-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(value, file, ensure_ascii=False)
        os.replace(temporary, path)  # mkstemp permissions are 0600.
    finally:
        Path(temporary).unlink(missing_ok=True)


async def publish(torrents, state_dir):
    target = Path(state_dir)/'torrent-stats.json'
    while True:
        try:
            value = snapshot(torrents)
            await asyncio.to_thread(write_snapshot, target, value)
        except Exception:
            log.exception('Could not publish local torrent telemetry')
        await asyncio.sleep(5)
