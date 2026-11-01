"""Opt-in synthetic AVI fixture: install while MCore is stopped, as mcore.

Only the initial 24 MiB and final 8 MiB are present in a sparse cache file.
No external tracker or user media is used; unavailable pieces remain absent.
"""
import asyncio
from pathlib import Path
import subprocess
import time
import libtorrent as lt
from mcore.config import load
from mcore.store import Store
from mcore.catalog import Catalog, normalize

config = load()
store = Store(config['state_dir'])
folder = Path(config['state_dir'])/'incomplete-demo'
folder.mkdir(exist_ok=True)
video = folder/'MCore-incomplete-test.avi'
if not video.exists():
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=24',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                    '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000',
                    '-map', '0:v', '-map', '1:a', '-map', '2:a', '-t', '120',
                    '-c:v', 'mpeg4', '-q:v', '2', '-c:a', 'ac3', '-b:a', '192k', str(video)], check=True)
size = video.stat().st_size
assert size > 40*1024*1024, size
files = lt.file_storage()
files.add_file(video.name, size)
torrent = lt.create_torrent(files, 262144)
torrent.set_priv(True)
lt.set_piece_hashes(torrent, str(folder))
metadata = bytes(lt.bencode(torrent.generate()))
info = lt.torrent_info(metadata)
key = str(info.info_hashes().get_best())
target = Path(config['cache_dir'])/key
target.mkdir(exist_ok=True)
tail = ((size-8*1024*1024)//262144)*262144
with video.open('rb') as source, (target/video.name).open('wb') as destination:
    destination.truncate(size)
    destination.write(source.read(24*1024*1024))
    source.seek(tail); destination.seek(tail); destination.write(source.read())
resume = Path(config['state_dir'])/'resume'
(resume/(key+'.torrent')).write_bytes(metadata)
(resume/(key+'.resume')).unlink(missing_ok=True)
magnet = lt.make_magnet_uri(info)
with store.connect() as db:
    db.execute('INSERT OR REPLACE INTO torrents VALUES (?,?,?,?)', (key, magnet, time.time(), size))
catalog = Catalog(store, config)
release = normalize('local', {'Name': 'MCore incomplete AVI (2026) 720p', 'Id': '2', 'Size': f'{size/1024**2:.1f} MiB'})
release.update(magnet=magnet, torrent_id=key, description='Синтетический AVI: доступны только начало и конец файла. Две аудиодорожки.', audio='AC3 · 440 Гц / 880 Гц')
catalog.put(release)
asyncio.run(catalog.close())
print('Installed incomplete AVI:', key, 'size:', size, 'available:', 24*1024*1024+size-tail, 'movie:', release['movie'])
