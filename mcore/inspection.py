import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
import libtorrent as lt
from .torrents import byte_range


class Inspection:
    def __init__(self, torrents, directory):
        self.torrents = torrents
        self.root = Path(directory)/'frames'
        self.root.mkdir(exist_ok=True)
        self.limit = asyncio.Semaphore(2)
        self.info = {}
        self.pending = {}
        self.retry_at = {}
        with torrents.store.connect() as db:
            for row in db.execute('SELECT * FROM media_info'):
                self.info[(row['torrent'], row['file'])] = json.loads(row['data'])
        torrents.media_info = self.info

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def schedule(self, key, index):
        identity = (key, index)
        if identity in self.info or identity in self.pending or self.retry_at.get(identity, 0) > time.monotonic():
            return
        async def run():
            try:
                await self.metadata(key, index)
            except (ValueError, KeyError, OSError, TimeoutError):
                self.retry_at[identity] = time.monotonic()+15
            finally:
                self.pending.pop(identity, None)
        self.pending[identity] = asyncio.create_task(run())

    @asynccontextmanager
    async def source(self, key, index):
        """Serve only verified pieces to ffprobe; a sparse hole is never interpreted as media."""
        handle, info, path, size, offset = self.torrents.file(key, index)
        token = '/'+secrets.token_hex(24)
        clients = set()
        budget = [32*1024*1024]
        self.torrents.active[key] = self.torrents.active.get(key, 0)+1
        self.torrents.last_used[key] = time.monotonic()
        async def serve(reader, writer):
            task = asyncio.current_task(); clients.add(task)
            try:
                header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
                lines = header.decode('ascii').split('\r\n')
                method, url, _ = lines[0].split(' ')
                if url != token or method not in ('GET', 'HEAD'):
                    writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n'); return
                headers = dict(line.lower().split(':', 1) for line in lines[1:] if ':' in line)
                start, end, code = byte_range(headers.get('range', '').strip(), size)
                piece = (offset+start)//info.piece_length()
                if not handle.have_piece(piece):
                    for p in range(piece, min(info.num_pieces(), piece+max(1, 8*1024*1024//info.piece_length()))):
                        handle.set_piece_deadline(p, 0)
                    writer.write(b'HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n'); return
                response = f'HTTP/1.1 {code} OK\r\nContent-Length: {end-start+1}\r\nAccept-Ranges: bytes\r\nConnection: close\r\n'
                if code == 206:
                    response += f'Content-Range: bytes {start}-{end}/{size}\r\n'
                writer.write((response+'\r\n').encode('ascii'))
                if method == 'HEAD':
                    return
                with path.open('rb') as source:
                    source.seek(start)
                    cursor = start
                    while cursor <= end and budget[0] > 0:
                        p = (offset+cursor)//info.piece_length()
                        if not handle.have_piece(p):
                            handle.set_piece_deadline(p, 0); break
                        count = min(65536, end-cursor+1, info.piece_length()-(offset+cursor)%info.piece_length(), budget[0])
                        chunk = await asyncio.to_thread(source.read, count)
                        if len(chunk) != count:
                            break
                        writer.write(chunk); await writer.drain()
                        budget[0] -= count; cursor += count
            except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                clients.discard(task)
        server = await asyncio.start_server(serve, '127.0.0.1', 0, limit=8192)
        try:
            yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}{token}'
        finally:
            server.close(); await server.wait_closed()
            for task in list(clients):
                task.cancel()
            await asyncio.gather(*list(clients), return_exceptions=True)
            self.torrents.active[key] -= 1

    def complete_file(self, key, index):
        handle, info, path, size, offset = self.torrents.file(key, index)
        first, last = offset//info.piece_length(), (offset+size-1)//info.piece_length()
        if not size or any(not handle.have_piece(piece) for piece in range(first, last+1)):
            raise ValueError('File must be fully cached for media inspection')
        return path

    async def process(self, *args):
        async with self.limit:
            process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
                if process.returncode:
                    raise ValueError('Media inspection failed')
                return output
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def metadata(self, key, index):
        identity = (key, index)
        if identity not in self.info:
            async with self.source(key, index) as url:
                raw = await self.process('ffprobe', '-v', 'error', '-protocol_whitelist', 'http,tcp',
                                         '-format_whitelist', 'avi,matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,mpegts',
                                         '-analyzeduration', '2000000', '-probesize', '2097152',
                                         '-show_entries', 'format=duration,bit_rate:stream=index,codec_type,codec_name,width,height,duration,bit_rate:stream_tags=language,title',
                                         '-of', 'json', url)
            data = json.loads(raw)
            if not data.get('streams'):
                raise ValueError('Streams not yet available')
            duration = float(data.get('format', {}).get('duration', 0))
            if not 0 < duration <= 604800:
                raise ValueError('Duration not yet available')
            data['duration_ms'] = round(duration*1000)
            size = self.torrents.file(key, index)[3]
            data['average_bitrate'] = round(size*8/duration)
            self.info[identity] = data
            with self.torrents.store.connect() as db:
                db.execute('INSERT OR REPLACE INTO media_info VALUES (?,?,?)', (key, index, json.dumps(data)))
        return self.info[identity]

    async def frame(self, release, index):
        key = release.get('torrent_id')
        if not key:
            params = lt.parse_magnet_uri(release['magnet'])
            key = str(params.info_hashes.get_best())
        files = self.torrents.files(key)
        if not files:
            raise ValueError('No cached video')
        file = max(files, key=lambda f: f['size'])
        self.torrents.status(key, file['index'], seconds=15, percent=0)  # Screenshot probing is not a playback startup request.
        metadata = await self.metadata(key, file['index'])
        duration = float(metadata.get('format', {}).get('duration', 0))
        position = max(0, duration*(index+1)/4)
        handle, info, _, size, offset = self.torrents.file(key, file['index'])
        expected = (offset+min(size-1, int(size*position/max(duration, 1))))//info.piece_length()
        if not handle.have_piece(expected):
            position = min(duration/2, (index+1)*2)
        # Output is kept in memory; media cache eviction therefore cannot leak an unbounded frame cache.
        async with self.source(key, file['index']) as url:
            return await self.process('ffmpeg', '-v', 'error', '-threads', '1', '-protocol_whitelist', 'http,tcp,pipe',
                                      '-format_whitelist', 'avi,matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,mpegts',
                                      '-ss', str(position), '-i', url, '-frames:v', '1',
                                      '-vf', 'scale=640:-2', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1')
