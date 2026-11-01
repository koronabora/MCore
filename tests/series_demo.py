"""Install a private synthetic two-episode MKV/MP4 fixture while MCore is stopped."""
import asyncio
from pathlib import Path
import shutil
import subprocess
import time
import libtorrent as lt
from mcore.config import load
from mcore.store import Store
from mcore.catalog import Catalog, normalize

config = load(); store = Store(config['state_dir'])
root = Path(config['state_dir'])/'series-demo'; root.mkdir(exist_ok=True)
subtitle = root/'captions.srt'
subtitle.write_text('1\n00:00:01,000 --> 00:00:09,000\nMCore episode subtitle test\n', encoding='utf-8')
for index, extension in ((1, 'mkv'), (2, 'mp4')):
    target = root/f'MCore.Series.S01E{index:02}.{extension}'
    if target.exists():
        continue
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24',
                    '-f', 'lavfi', '-i', f'sine=frequency={440*index}:sample_rate=48000',
                    '-i', str(subtitle), '-map', '0:v', '-map', '1:a', '-map', '2:s', '-t', '20',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-c:a', 'aac',
                    '-c:s', 'srt' if extension == 'mkv' else 'mov_text', '-metadata:s:a:0', 'language=rus',
                    '-metadata:s:s:0', 'language=eng', str(target)], check=True)
files = lt.file_storage()
for video in sorted(root.glob('MCore.Series.*')):
    files.add_file(root.name+'/'+video.name, video.stat().st_size)
torrent = lt.create_torrent(files, 262144, flags=lt.create_torrent.v1_only); torrent.set_priv(True); lt.set_piece_hashes(torrent, str(root.parent))
metadata = bytes(lt.bencode(torrent.generate())); info = lt.torrent_info(metadata)
key = str(info.info_hashes().get_best()); magnet = lt.make_magnet_uri(info)
destination = Path(config['cache_dir'])/key/root.name; destination.mkdir(parents=True, exist_ok=True)
for video in root.glob('MCore.Series.*'):
    shutil.copy2(video, destination/video.name)
resume = Path(config['state_dir'])/'resume'; resume.mkdir(exist_ok=True)
(resume/(key+'.torrent')).write_bytes(metadata)
with store.connect() as db:
    db.execute('INSERT OR IGNORE INTO torrents VALUES (?,?,?,?)', (key, magnet, time.time(), info.total_size()))
catalog = Catalog(store, config)
release = normalize('local', {'Id': '3', 'Title': 'MCore series demo S01 (2026) 360p', 'Size': f'{info.total_size()/1024**2:.1f} MiB'})
release.update(magnet=magnet, torrent_id=key, description='Две синтетические серии: MKV и MP4, аудио и встроенные субтитры.')
catalog.put(release); asyncio.run(catalog.close())
print('SERIES', key, release['movie'])
