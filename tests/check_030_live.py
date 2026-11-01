"""Opt-in integrated check, exclusively synthetic video plus public archive listings."""
import asyncio
import ssl
import time
import httpx
from mcore.config import load
from mcore.store import Store, digest
from mcore.metadata import Metadata
from mcore.archive import page

config = load(); store = Store(config['state_dir'])
with httpx.Client(base_url=config['public_url'], verify=ssl.create_default_context(cafile=config['tls_cert']), timeout=90) as client:
    key = client.post('/v1/pair', json={'token': store.invite('emulator-test')}).json()['key']
    client.headers['Authorization'] = 'Bearer '+key
    try:
        assert client.get('/v1/health').json()['version'] == '0.3.0'
        torrent = client.post('/v1/torrents', json={'release': 'local:2'}).json()['id']
        info = client.get(f'/v1/torrents/{torrent}/files/0/info'); info.raise_for_status(); info = info.json()
        assert 119000 < info['duration_ms'] < 121000 and len([s for s in info['streams'] if s['codec_type'] == 'audio']) == 2
        start = client.get(f'/v1/torrents/{torrent}?buffer=5').json()
        middle = client.get(f'/v1/torrents/{torrent}?buffer=5&position=60000').json()
        tail = client.get(f'/v1/torrents/{torrent}?buffer=5&position=118000').json()
        assert start['ready'] and not middle['ready'] and tail['ready'] and start['progress'] < .3
        assert start['buffer_basis'] == 'average_bitrate'
        frame = client.get('/v1/releases/local:2/screenshots/0'); frame.raise_for_status(); assert frame.content.startswith(b'\xff\xd8')
        print('PASS partial AVI metadata, actual bitrate buffer, resume window and frame', flush=True)
        series = client.post('/v1/torrents', json={'release': 'local:3'}).json()['id']
        files = client.get('/v1/torrents/'+series).json()['files']
        assert len(files) == 2 and files[0]['episode'] != files[1]['episode']
        for file in files:
            media = client.get(f"/v1/torrents/{series}/files/{file['index']}/info"); media.raise_for_status()
            assert any(s['codec_type'] == 'subtitle' for s in media.json()['streams'])
        print('PASS series MKV/MP4 metadata and subtitle streams', flush=True)
    finally:
        with store.connect() as db:
            db.execute('DELETE FROM sessions WHERE hash=?', (digest(key),))

async def archives():
    metadata = Metadata(config['jackett'])
    try:
        for provider, first, second in [('kinozal', '/browse.php?c=1002&page=0', '/browse.php?c=1002&page=1'),
                                        ('rutracker', '/forum/viewforum.php?f=2090', '/forum/viewforum.php?f=2090&start=50')]:
            one, links = await page(metadata, provider, first); two, _ = await page(metadata, provider, second)
            assert one and two and {r['Id'] for r in one} != {r['Id'] for r in two} and second in links
            print('PASS paginated archive', provider, len(one), len(two), flush=True)
    finally:
        await metadata.client.aclose()
asyncio.run(archives())
