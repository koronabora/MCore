import asyncio
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
import libtorrent as lt
from .network import session_settings, validate_torrent

log = logging.getLogger(__name__)
VIDEO = {'.mkv', '.mp4', '.avi', '.webm', '.mov', '.m4v', '.ts'}


def cache_path(config):
    marker = Path(config['state_dir']) / 'cache-location.json'
    desired = Path(config['cache_dir']).resolve()
    old = Path(json.loads(marker.read_text())) if marker.exists() else desired
    if old != desired and old.exists():
        if old in desired.parents or desired in old.parents:
            raise ValueError('Cache directories must not be nested')
        try:
            desired.parent.mkdir(parents=True, exist_ok=True)
            if desired.exists():
                raise OSError('Destination already exists; refusing to overwrite')
            # shutil.move copies across filesystems; on failure the old location is retained.
            stage = desired.with_name(desired.name + '.mcore-migration')
            if stage.exists():
                raise OSError('Unfinished migration requires inspection: ' + str(stage))
            shutil.copytree(old, stage)
            stage.rename(desired)
        except OSError:
            log.exception('Cache migration failed; retaining previous directory')
            desired = old
    desired.mkdir(parents=True, exist_ok=True)
    temp = marker.with_suffix('.tmp')
    temp.write_text(json.dumps(str(desired)))
    os.replace(temp, marker)
    if old != desired and old.exists():
        shutil.rmtree(old)
    return desired


def byte_range(header, size):
    if not header:
        return 0, size - 1, 200
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()):
        raise ValueError('Invalid range')
    a, b = match.groups()
    if not a:
        length = int(b)
        if length <= 0:
            raise ValueError('Invalid suffix')
        start, end = max(0, size-length), size-1
    else:
        start, end = int(a), min(int(b), size-1) if b else size-1
    if start >= size or end < start:
        raise ValueError('Unsatisfiable range')
    return start, end, 206


class Torrents:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.root = cache_path(config)
        self.resume = Path(config['state_dir']) / 'resume'
        self.resume.mkdir(exist_ok=True)
        self.network = validate_torrent(config.get('torrent', {}), config['port'])
        settings = session_settings(self.network)
        settings['alert_mask'] = int(lt.alert.category_t.storage_notification | lt.alert.category_t.status_notification | lt.alert.category_t.error_notification)
        self.session = lt.session(settings)
        self._check_listener()
        self.handles = {}
        self.active = {}
        self.lock = asyncio.Lock()
        with store.connect() as db:
            rows = db.execute('SELECT * FROM torrents').fetchall()
        for row in rows:
            try:
                path = self.resume / (row['id'] + '.resume')
                params = lt.read_resume_data(path.read_bytes()) if path.exists() else lt.parse_magnet_uri(row['magnet'])
                params.save_path = str(self.root / row['id'])
                self._apply_peer_settings(params)
                self.handles[row['id']] = self.session.add_torrent(params)
            except Exception:
                log.exception('Could not restore torrent %s', row['id'])

    def _check_listener(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.session.wait_for_alert(250)
            for alert in self.session.pop_alerts():
                if isinstance(alert, lt.listen_failed_alert):
                    raise RuntimeError('BitTorrent listener failed: ' + alert.message())
            if self.session.is_listening() and self.session.listen_port() == self.network['listen_port']:
                log.info('BitTorrent listening on %s', self.network['listen_port'])
                return
        raise RuntimeError('BitTorrent could not bind the configured port')

    def _apply_peer_settings(self, params):
        if self.network['enable_pex']:
            params.flags &= ~lt.torrent_flags.disable_pex
        else:
            params.flags |= lt.torrent_flags.disable_pex

    async def add(self, magnet):
        if not magnet.startswith('magnet:?'):
            raise ValueError('Source has no magnet link')
        params = lt.parse_magnet_uri(magnet)
        self._apply_peer_settings(params)
        key = str(params.info_hashes.get_best())
        async with self.lock:
            if key not in self.handles:
                params.save_path = str(self.root / key)
                params.flags |= lt.torrent_flags.sequential_download
                params.flags &= ~lt.torrent_flags.auto_managed
                params.flags &= ~lt.torrent_flags.paused
                handle = self.session.add_torrent(params)
                self.handles[key] = handle
                with self.store.connect() as db:
                    db.execute('INSERT OR IGNORE INTO torrents(id,magnet,added) VALUES (?,?,?)', (key, magnet, time.time()))
            return key

    def files(self, key):
        handle = self.handles[key]
        info = handle.torrent_file()
        if info is None:
            return []
        files = info.files()
        return [{'index': i, 'name': files.file_path(i), 'size': files.file_size(i)} for i in range(files.num_files())
                if Path(files.file_path(i)).suffix.lower() in VIDEO]

    def file(self, key, index):
        handle = self.handles[key]
        info = handle.torrent_file()
        if info is None:
            raise ValueError('Metadata not ready')
        files = info.files()
        if index < 0 or index >= files.num_files() or files.pad_file_at(index):
            raise ValueError('Invalid file index')
        path = (self.root / key / files.file_path(index)).resolve()
        if not path.is_relative_to((self.root / key).resolve()):
            raise ValueError('Unsafe torrent file path')
        return handle, info, path, files.file_size(index), files.file_offset(index)

    def status(self, key, index=0, seconds=15):
        handle = self.handles[key]
        status = handle.status()
        result = {'id': key, 'progress': status.progress, 'download_rate': status.download_payload_rate,
                  'seeds': status.num_seeds, 'peers': status.num_peers, 'files': self.files(key),
                  'ready': False, 'buffer_eta': None, 'error': str(status.errc.message()) if status.errc.value() else None}
        if status.has_metadata:
            _, info, _, size, offset = self.file(key, index)
            # Conservative estimate until ffprobe supplies actual duration/bitrate.
            target = min(size, max(4*1024*1024, seconds*1024*1024))
            first, last = offset // info.piece_length(), (offset+target-1) // info.piece_length()
            missing = sum(info.piece_size(i) for i in range(first, last+1) if not handle.have_piece(i))
            for i in range(first, last+1):
                if not handle.have_piece(i):
                    handle.set_piece_deadline(i, (i-first)*50)
            tail = (offset+size-1) // info.piece_length()
            if size and not handle.have_piece(tail):
                handle.set_piece_deadline(tail, 0)
                missing += info.piece_size(tail)
            result['ready'] = missing == 0 and size > 0
            result['buffer_eta'] = round(missing / status.download_payload_rate) if status.download_payload_rate else None
            result['file_size'] = size
        return result

    async def stream(self, key, index, start, end):
        handle, info, path, _, offset = self.file(key, index)
        self.active[key] = self.active.get(key, 0)+1
        try:
            cursor = start
            while cursor <= end:
                piece = (offset+cursor) // info.piece_length()
                length = min(end-cursor+1, info.piece_length() - (offset+cursor) % info.piece_length(), 256*1024)
                deadline = time.monotonic()+90
                handle.set_piece_deadline(piece, 0)
                while not handle.have_piece(piece):
                    if time.monotonic() > deadline:
                        raise TimeoutError('Torrent piece unavailable')
                    await asyncio.sleep(.15)
                def read():
                    with path.open('rb') as f:
                        f.seek(cursor)
                        return f.read(length)
                chunk = await asyncio.to_thread(read)
                if len(chunk) != length:
                    raise IOError('Short torrent file read')
                cursor += len(chunk)
                yield chunk
        finally:
            self.active[key] -= 1

    async def maintain(self):
        while True:
            try:
                async with self.lock:
                    with self.store.connect() as db:
                        for key, handle in list(self.handles.items()):
                            info = handle.torrent_file()
                            if info:
                                db.execute('UPDATE torrents SET size=? WHERE id=?', (info.total_size(), key))
                            handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
                        rows = db.execute('SELECT * FROM torrents ORDER BY added').fetchall()
                        reserved = sum(row['size'] for row in rows)
                        for row in rows:
                            if reserved <= self.config['cache_bytes']:
                                break
                            key = row['id']
                            handle = self.handles.get(key)
                            if handle and not self.active.get(key) and handle.status().is_seeding:
                                self.session.remove_torrent(handle, lt.options_t.delete_files)
                                del self.handles[key]
                                db.execute('DELETE FROM torrents WHERE id=?', (key,))
                                (self.resume / (key+'.resume')).unlink(missing_ok=True)
                                reserved -= row['size']
                        # When active jobs exceed the budget, pause them rather than overfill disk.
                        for key, handle in self.handles.items():
                            if reserved > self.config['cache_bytes'] or shutil.disk_usage(self.root).free < 1024**3:
                                handle.pause()
                            elif handle.status().paused:
                                handle.resume()
                    for alert in self.session.pop_alerts():
                        if isinstance(alert, lt.save_resume_data_alert):
                            key = str(alert.params.info_hashes.get_best())
                            target = self.resume / (key+'.resume')
                            temp = target.with_suffix('.tmp')
                            temp.write_bytes(lt.write_resume_data_buf(alert.params))
                            temp.replace(target)
            except Exception:
                log.exception('Torrent maintenance failed')
            await asyncio.sleep(5)
