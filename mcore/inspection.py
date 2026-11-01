import asyncio
import json
from pathlib import Path
import libtorrent as lt


class Inspection:
    def __init__(self, torrents, directory):
        self.torrents = torrents
        self.root = Path(directory)/'frames'
        self.root.mkdir(exist_ok=True)
        self.limit = asyncio.Semaphore(2)
        self.info = {}

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
        path = self.complete_file(key, index)
        identity = (key, index)
        if identity not in self.info:
            raw = await self.process('ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                     '-show_entries', 'format=duration:stream=index,codec_type,codec_name,width,height:stream_tags=language,title',
                                     '-of', 'json', str(path))
            self.info[identity] = json.loads(raw)
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
        path = self.complete_file(key, file['index'])
        self.torrents.status(key, file['index'])  # Protect the source against eviction while in use.
        metadata = await self.metadata(key, file['index'])
        duration = float(metadata.get('format', {}).get('duration', 0))
        position = max(0, duration*(index+1)/4)
        # Output is kept in memory; media cache eviction therefore cannot leak an unbounded frame cache.
        return await self.process('ffmpeg', '-v', 'error', '-threads', '1', '-protocol_whitelist', 'file,pipe',
                                  '-ss', str(position), '-i', str(path), '-frames:v', '1',
                                  '-vf', 'scale=640:-2', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1')
