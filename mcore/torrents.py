import asyncio
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
import math
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
        self.errors = {}
        self.media_info = {}
        self.last_used = {}
        self.lock = asyncio.Lock()
        with store.connect() as db:
            rows = db.execute('SELECT * FROM torrents').fetchall()
        for row in rows:
            try:
                path = self.resume / (row['id'] + '.resume')
                metadata = self.resume / (row['id'] + '.torrent')
                if path.exists():
                    params = lt.read_resume_data(path.read_bytes())
                elif metadata.exists():
                    params = lt.add_torrent_params()
                    params.ti = lt.torrent_info(metadata.read_bytes())
                else:
                    params = lt.parse_magnet_uri(row['magnet'])
                params.save_path = str(self.root / row['id'])
                params.flags |= lt.torrent_flags.upload_mode
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
        metadata = magnet if isinstance(magnet, bytes) else None
        if metadata is not None:
            params = lt.add_torrent_params()
            params.ti = lt.torrent_info(metadata)
            magnet = lt.make_magnet_uri(params.ti)
            key = str(params.ti.info_hashes().get_best())
        else:
            if not magnet.startswith('magnet:?'):
                raise ValueError('Source has no magnet link')
            params = lt.parse_magnet_uri(magnet)
            key = str(params.info_hashes.get_best())
        self._apply_peer_settings(params)
        async with self.lock:
            if key not in self.handles:
                if metadata is not None:
                    (self.resume / (key + '.torrent')).write_bytes(metadata)
                params.save_path = str(self.root / key)
                params.flags |= lt.torrent_flags.sequential_download
                # Metadata is allowed; payload starts only after reserving disk budget.
                params.flags |= lt.torrent_flags.upload_mode
                params.flags &= ~lt.torrent_flags.auto_managed
                params.flags &= ~lt.torrent_flags.paused
                handle = self.session.add_torrent(params)
                self.handles[key] = handle
                self.last_used[key] = time.monotonic()
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
        if index < 0 or index >= files.num_files() or files.file_flags(index) & lt.file_storage.flag_pad_file:
            raise ValueError('Invalid file index')
        path = (self.root / key / files.file_path(index)).resolve()
        if not path.is_relative_to((self.root / key).resolve()):
            raise ValueError('Unsafe torrent file path')
        return handle, info, path, files.file_size(index), files.file_offset(index)

    def status(self, key, index=0, seconds=15, position=0):
        handle = self.handles[key]
        self.last_used[key] = time.monotonic()
        status = handle.status()
        result = {'id': key, 'progress': status.progress, 'download_rate': status.download_payload_rate,
                  'seeds': status.num_seeds, 'peers': status.num_peers, 'files': self.files(key),
                  'ready': False, 'buffer_eta': None,
                  'error': self.errors.get(key) or (str(status.errc.message()) if status.errc.value() else None)}
        if status.has_metadata:
            _, info, _, size, offset = self.file(key, index)
            metadata = self.media_info.get((key, index), {})
            duration = metadata.get('duration_ms', 0)/1000
            # Duration comes from the container, not a fixed bytes-per-second guess.
            # VBR needs a margin; actual decoder reads remain authoritative after seeking.
            rate = size/duration if duration else 1024*1024
            start = max(0, min(size-1, int(max(0, position/1000-2)*rate))) if duration else 0
            target = min(size-start, max(1024*1024, math.ceil((seconds+2)*rate*1.25)) if duration else max(4*1024*1024, seconds*1024*1024))
            first, last = (offset+start)//info.piece_length(), (offset+start+target-1)//info.piece_length()
            wanted = set(range(first, last+1))
            # Header pieces are also required for a resumed decoder to reopen the container.
            wanted.update(range(offset//info.piece_length(), (offset+min(size, 1024*1024)-1)//info.piece_length()+1))
            # AVI/OpenDML and MP4 indexes can span several pieces, not just the last one.
            tail_first = (offset+max(0, size-8*1024*1024)) // info.piece_length()
            tail_last = (offset+size-1) // info.piece_length()
            wanted.update(range(tail_first, tail_last+1))
            missing = 0
            for piece in sorted(wanted):
                if size and not handle.have_piece(piece):
                    handle.set_piece_deadline(piece, 0)
                    missing += info.piece_size(piece)
            result['ready'] = missing == 0 and size > 0 and (position == 0 or duration > 0)
            result['buffer_eta'] = round(missing / status.download_payload_rate) if status.download_payload_rate else None
            result['file_size'] = size
            result.update(buffer_basis='average_bitrate' if duration else 'fallback', duration_ms=metadata.get('duration_ms'),
                          buffer_position=position, buffer_bytes=target, buffer_missing_bytes=missing, media=metadata or None)
        return result

    async def stream(self, key, index, start, end):
        handle, info, path, _, offset = self.file(key, index)
        self.active[key] = self.active.get(key, 0)+1
        self.last_used[key] = time.monotonic()
        try:
            cursor = start
            while cursor <= end:
                piece = (offset+cursor) // info.piece_length()
                length = min(end-cursor+1, info.piece_length() - (offset+cursor) % info.piece_length(), 256*1024)
                deadline = time.monotonic()+90
                # Read-ahead follows the player's current position, including after a seek.
                ahead = min(info.num_pieces()-1, (offset+min(end, cursor+8*1024*1024)) // info.piece_length())
                for upcoming in range(piece, ahead+1):
                    if not handle.have_piece(upcoming):
                        handle.set_piece_deadline(upcoming, (upcoming-piece)*50)
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
                        rows = db.execute('SELECT * FROM torrents ORDER BY added').fetchall()
                        reserved = sum(row['size'] for row in rows)
                        requested = sum(h.torrent_file().total_size() for key, h in self.handles.items()
                                        if h.torrent_file() and not next((r['size'] for r in rows if r['id']==key), 0)
                                        and h.torrent_file().total_size() <= self.config['cache_bytes'])
                        for row in rows:
                            if reserved + requested <= self.config['cache_bytes']:
                                break
                            key = row['id']
                            handle = self.handles.get(key)
                            if (handle and not self.active.get(key) and handle.status().is_seeding
                                    and time.monotonic()-self.last_used.get(key, 0) > 1800):
                                self.session.remove_torrent(handle, lt.options_t.delete_files)
                                del self.handles[key]
                                db.execute('DELETE FROM torrents WHERE id=?', (key,))
                                (self.resume / (key+'.resume')).unlink(missing_ok=True)
                                (self.resume / (key+'.torrent')).unlink(missing_ok=True)
                                reserved -= row['size']
                        sizes = {r['id']: r['size'] for r in rows}
                        for key, handle in self.handles.items():
                            info = handle.torrent_file()
                            if not info:
                                continue
                            if not sizes.get(key):
                                needed = info.total_size()
                                if reserved + needed > self.config['cache_bytes']:
                                    handle.set_upload_mode(True)
                                    self.errors[key] = 'Cache capacity exhausted; increase cache_bytes or wait for eviction'
                                    continue
                                reserved += needed
                                db.execute('UPDATE torrents SET size=? WHERE id=?', (needed, key))
                            if reserved > self.config['cache_bytes'] or shutil.disk_usage(self.root).free < 1024**3:
                                handle.pause()
                                self.errors[key] = 'Insufficient cache budget or free disk space'
                            else:
                                handle.set_upload_mode(False)
                                self.errors.pop(key, None)
                                if handle.status().paused:
                                    handle.resume()
                            if handle.need_save_resume_data():
                                handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
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
