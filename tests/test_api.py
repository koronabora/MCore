import hashlib
import json
import socket
import time
import libtorrent as lt
import pytest
from fastapi.testclient import TestClient
from mcore.api import app
from mcore.catalog import normalize


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = tmp_path / 'config.toml'
    config.write_text(f'''port = 45873
public_url = "https://localhost:45873"
state_dir = "{tmp_path / 'state'}"
cache_dir = "{tmp_path / 'cache'}"
cache_bytes = 104857600
sync_seconds = 21600
torapi_url = "http://127.0.0.1:1"
providers = []
[torrent]
listen_port = {free_port()}
listen_addresses = ["127.0.0.1"]
enable_dht = false
''')
    monkeypatch.setenv('MCORE_CONFIG', str(config))
    with TestClient(app) as client:
        yield client


def login(client, name):
    app.state.store.create_user(name)
    invite = app.state.store.invite(name)
    response = client.post('/v1/pair', json={'token': invite, 'device': 'test'})
    assert response.status_code == 200
    assert client.post('/v1/pair', json={'token': invite}).status_code == 401
    return {'Authorization': 'Bearer ' + response.json()['key']}


def test_auth_history_isolation_and_reinstallation(client):
    assert client.get('/v1/home').status_code == 401
    first, other = login(client, 'first'), login(client, 'other')
    release = normalize('rutor', {'Name': 'Test movie (2024) 1080p', 'Id': '123'})
    app.state.catalog.put(release)
    mid = release['movie']
    assert client.put('/v1/history/' + mid, headers=first, json={'position': 12345}).status_code == 200
    assert client.get('/v1/home', headers=other).json()['history'] == []
    response = client.post('/v1/pair', json={'token': app.state.store.invite('first')})
    restored = {'Authorization': 'Bearer ' + response.json()['key']}
    assert client.get('/v1/home', headers=restored).json()['history'][0]['position'] == 12345
    assert client.get('/docs').status_code == 404


def test_real_local_torrent_transfer_and_http_ranges(client, tmp_path, monkeypatch):
    auth = login(client, 'streamer')
    source = tmp_path/'seed'
    source.mkdir()
    payload = bytes(range(256))*8192
    (source/'sample.mkv').write_bytes(payload)
    files = lt.file_storage()
    files.add_file('sample.mkv', len(payload))
    torrent = lt.create_torrent(files, 16384)
    lt.set_piece_hashes(torrent, str(source))
    info = lt.torrent_info(torrent.generate())
    seed = lt.session({'listen_interfaces': f'127.0.0.1:{free_port()}', 'enable_dht': False,
                       'enable_lsd': False, 'enable_upnp': False, 'enable_natpmp': False})
    params = lt.add_torrent_params()
    params.ti, params.save_path = info, str(source)
    params.flags |= lt.torrent_flags.seed_mode
    params.flags &= ~lt.torrent_flags.paused
    handle = seed.add_torrent(params)
    async def detail(_):
        return {'magnet': lt.make_magnet_uri(info)}
    monkeypatch.setattr(app.state.catalog, 'detail', detail)
    try:
        response = client.post('/v1/torrents', headers=auth, json={'release': 'local:1'})
        assert response.status_code == 200, response.text
        key = response.json()['id']
        download = app.state.torrents.handles[key]
        deadline = time.monotonic()+30
        while not seed.is_listening() and time.monotonic()<deadline:
            time.sleep(.05)
        download.connect_peer(('127.0.0.1', seed.listen_port()))
        while not download.status().is_seeding and time.monotonic()<deadline:
            time.sleep(.1)
        assert download.status().is_seeding, str(download.status().errc)
        path = f'/v1/torrents/{key}/files/0'
        status = client.get(f'/v1/torrents/{key}', headers=auth)
        assert status.status_code == 200, status.text
        assert status.json()['ready']
        assert client.get(path).status_code == 401
        head = client.head(path, headers=auth)
        assert int(head.headers['Content-Length']) == len(payload)
        response = client.get(path, headers=auth | {'Range': 'bytes=16380-16400'})
        assert response.status_code == 206
        assert response.content == payload[16380:16401]
        response = client.get(path, headers=auth | {'Range': 'bytes=-100'})
        assert response.content == payload[-100:]
        assert client.get(path, headers=auth | {'Range': f'bytes={len(payload)}-'}).status_code == 416
        whole = client.get(path, headers=auth)
        assert hashlib.sha256(whole.content).digest() == hashlib.sha256(payload).digest()
    finally:
        seed.pause()
