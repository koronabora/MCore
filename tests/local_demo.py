"""Install an explicitly synthetic clip for offline Android integration testing.

Run as mcore with the service stopped. No tracker or external media is used.
"""
import asyncio
from pathlib import Path
import shutil
import subprocess
import time
import libtorrent as lt
from mcore.config import load
from mcore.store import Store
from mcore.catalog import Catalog, normalize

config = load()
store = Store(config['state_dir'])
folder = Path(config['state_dir']) / 'local-demo'
folder.mkdir(exist_ok=True)
video = folder / 'MCore-local-test.mp4'
subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                'testsrc2=size=1280x720:rate=24', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                '-t', '30', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30', '-c:a', 'aac',
                '-movflags', '+faststart', str(video)], check=True)
files = lt.file_storage()
files.add_file(video.name, video.stat().st_size)
torrent = lt.create_torrent(files, 262144)
lt.set_piece_hashes(torrent, str(folder))
info = lt.torrent_info(torrent.generate())
key = str(info.info_hashes().get_best())
target = Path(config['cache_dir']) / key
target.mkdir(parents=True, exist_ok=True)
shutil.copy2(video, target/video.name)
params = lt.add_torrent_params()
params.ti, params.save_path = info, str(target)
params.flags &= ~lt.torrent_flags.auto_managed
params.flags &= ~lt.torrent_flags.paused
resume = Path(config['state_dir']) / 'resume'
resume.mkdir(exist_ok=True)
(resume/(key+'.resume')).write_bytes(lt.write_resume_data_buf(params))
(resume/(key+'.torrent')).write_bytes(lt.bencode(torrent.generate()))
magnet = lt.make_magnet_uri(info)
with store.connect() as db:
    db.execute('INSERT OR REPLACE INTO torrents VALUES (?,?,?,?)', (key, magnet, time.time(), video.stat().st_size))
catalog = Catalog(store, config)
release = normalize('local', {'Name': 'MCore demo (2026) 720p', 'Id': '1', 'Seeds': 1, 'Size': f'{video.stat().st_size/1024**2:.1f} MiB'})
release.update(magnet=magnet, torrent_id=key, director='MCore / ffmpeg', description='Синтетический 30-секундный тестовый ролик. Создан локально для проверки воспроизведения, перемотки и скачивания.', audio='AAC · тестовый звуковой сигнал')
catalog.put(release)
asyncio.run(catalog.client.aclose())
print('Installed local test movie:', release['movie'])
