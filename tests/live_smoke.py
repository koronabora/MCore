"""Check the installed HTTPS service against the synthetic local demo."""
import hashlib
from pathlib import Path
import ssl
import httpx
from mcore.config import load
from mcore.store import Store

config = load()
store = Store(config['state_dir'])
token = store.invite('emulator-test')
with httpx.Client(base_url=config['public_url'], verify=ssl.create_default_context(cafile=config['tls_cert']), timeout=60, trust_env=False) as client:
    assert client.get('/v1/health').status_code == 401
    response = client.post('/v1/pair', json={'token': token})
    response.raise_for_status()
    client.headers['Authorization'] = 'Bearer ' + response.json()['key']
    assert client.post('/v1/pair', json={'token': token}).status_code == 401
    assert client.get('/v1/health').json()['status'] == 'ok'
    movie = client.get('/v1/search', params={'q': 'MCore', 'live': False}).json()['movies'][0]
    detail = client.get('/v1/movies/'+movie['id'])
    detail.raise_for_status()
    key = client.post('/v1/torrents', json={'release': 'local:1'}).json()['id']
    metadata = client.get(f'/v1/torrents/{key}/files/0/info')
    metadata.raise_for_status()
    assert any(s['codec_type']=='audio' for s in metadata.json()['streams'])
    screenshot = client.get('/v1/releases/local:1/screenshots/0')
    screenshot.raise_for_status()
    assert screenshot.content.startswith(b'\xff\xd8')
    video = client.get(f'/v1/torrents/{key}/files/0')
    video.raise_for_status()
    expected = (Path(config['state_dir'])/'local-demo/MCore-local-test.mp4').read_bytes()
    assert hashlib.sha256(video.content).digest() == hashlib.sha256(expected).digest()
    print('PASS: TLS verification, authentication, single-use invitation, catalog, audio metadata, frame, full video SHA-256')
    with store.connect() as db:
        from mcore.store import digest
        db.execute('DELETE FROM sessions WHERE hash=?', (digest(client.headers['Authorization'][7:]),))
