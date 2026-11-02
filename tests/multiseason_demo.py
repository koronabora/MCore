"""Private multi-season fixture, installed with MCore stopped after series_demo.py."""
import asyncio
from pathlib import Path
import shutil
import time
import libtorrent as lt
from mcore.config import load
from mcore.store import Store
from mcore.catalog import Catalog, normalize

config=load(); store=Store(config['state_dir'])
root=Path(config['state_dir'])/'multiseason-demo'; root.mkdir(exist_ok=True)
source=root.parent/'series-demo'
for season,episode in ((1,1),(2,1),(2,2)):
    shutil.copy2(source/'MCore.Series.S01E02.mp4',root/f'MCore.Multi.S{season:02}E{episode:02}.mp4')
files=lt.file_storage()
for video in sorted(root.glob('*.mp4')): files.add_file(root.name+'/'+video.name,video.stat().st_size)
torrent=lt.create_torrent(files,262144,flags=lt.create_torrent.v1_only); torrent.set_priv(True); lt.set_piece_hashes(torrent,str(root.parent))
metadata=bytes(lt.bencode(torrent.generate())); info=lt.torrent_info(metadata)
key=str(info.info_hashes().get_best()); magnet=lt.make_magnet_uri(info)
destination=Path(config['cache_dir'])/key/root.name; destination.mkdir(parents=True,exist_ok=True)
for video in root.glob('*.mp4'): shutil.copy2(video,destination/video.name)
(Path(config['state_dir'])/'resume'/(key+'.torrent')).write_bytes(metadata)
(Path(config['state_dir'])/'resume'/(key+'.resume')).unlink(missing_ok=True)
with store.connect() as db: db.execute('INSERT OR IGNORE INTO torrents VALUES (?,?,?,?)',(key,magnet,time.time(),info.total_size()))
catalog=Catalog(store,config)
release=normalize('local',{'Id':'4','Title':'MCore multiseason demo S01-02 (2026) 360p','Size':f'{info.total_size()/1024**2:.1f} MiB'})
release.update(magnet=magnet,torrent_id=key,series=True,season='1-2',description='Private synthetic two-season fixture.')
catalog.put(release); asyncio.run(catalog.close())
print('MULTISEASON',key,release['movie'])
