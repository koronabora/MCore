import hashlib
import asyncio
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


def test_metadata_index_uses_download_without_waiting_for_description(monkeypatch):
    from types import SimpleNamespace
    from mcore.api import Start, start_release
    release = dict(id='local:ready', download='test-link')
    async def forbidden(*args):
        raise AssertionError('Description fetch must not delay available torrent metadata')
    async def download(url):
        assert url == 'test-link'
        return b'torrent'
    async def add(source, metadata_only=False):
        assert source == b'torrent' and metadata_only
        return 'hash'
    catalog = SimpleNamespace(release=lambda rid: release, detail=forbidden,
                              jackett=SimpleNamespace(download=download), put=lambda r: None)
    monkeypatch.setattr(app.state, 'catalog', catalog, raising=False)
    monkeypatch.setattr(app.state, 'torrents', SimpleNamespace(handles={}, add=add), raising=False)
    assert asyncio.run(start_release(Start(release='local:ready'), metadata_only=True)) == {'id': 'hash'}


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
    release = normalize('local', {'Name': 'Test movie (2024) 1080p', 'Id': '123'})
    app.state.catalog.put(release)
    mid = release['movie']
    assert client.put('/v1/history/' + mid, headers=first, json={'position': 100, 'torrent': 'a'*40, 'file': 3}).status_code == 200
    assert client.put('/v1/history/' + mid, headers=first, json={'position': 12345}).status_code == 200
    with app.state.store.connect() as db:
        row = db.execute('SELECT torrent,file FROM history WHERE movie=?', (mid,)).fetchone()
        assert row['torrent'] == 'a'*40 and row['file'] == 3
    assert client.get('/v1/home', headers=other).json()['history'] == []
    response = client.post('/v1/pair', json={'token': app.state.store.invite('first')})
    restored = {'Authorization': 'Bearer ' + response.json()['key']}
    assert client.get('/v1/home', headers=restored).json()['history'][0]['position'] == 12345
    assert client.get('/docs').status_code == 404


def test_torrent_file_preserves_private_flag_and_metadata(client, tmp_path):
    files = lt.file_storage()
    files.add_file('private.mkv', 16384)
    torrent = lt.create_torrent(files, 16384)
    torrent.set_priv(True)
    torrent.add_tracker('http://127.0.0.1:1/announce')
    (tmp_path / 'private.mkv').write_bytes(b'\x01'*16384)
    lt.set_piece_hashes(torrent, str(tmp_path))
    metadata = bytes(lt.bencode(torrent.generate()))
    manager = app.state.torrents
    key = asyncio.run(manager.add(metadata))
    info = manager.handles[key].torrent_file()
    assert info.priv()
    assert info.files().file_path(0) == 'private.mkv'
    stored = manager.resume / (key + '.torrent')
    assert stored.read_bytes() == metadata
    assert lt.torrent_info(stored.read_bytes()).priv()


def test_history_alias_and_legacy_filename_match(client):
    auth = login(client, 'migration')
    old = normalize('local', {'Id': '45', 'Title': 'Example / Example original (2024) 1080p'})
    app.state.catalog.put(old)
    old_id = old['movie']
    client.put('/v1/history/'+old_id, headers=auth, json={'position': 1234})
    changed = dict(old, movie='a'*24)
    app.state.catalog.put(changed)
    response = client.get('/v1/movies/'+changed['movie'], headers=auth)
    assert response.status_code == 200 and response.json()['position'] == 1234
    assert client.put('/v1/history/'+old_id, headers=auth, json={'position': 5678}).status_code == 200
    matched = client.get('/v1/library/match', params={'name': 'Example original [2024 BDRip].avi'}, headers=auth)
    assert matched.json()['movie']['id'] == changed['movie']
    assert client.get('/v1/library/match', params={'name': 'Unknown.mp4'}, headers=auth).json()['movie'] is None


def test_independent_episode_progress_and_completion(client):
    from mcore.episodes import register
    auth, other = login(client, 'episodes'), login(client, 'episodes-other')
    release = normalize('local', {'Id': '71', 'Title': 'Episode fixture S01 2026 1080p'})
    app.state.catalog.put(release)
    key = 'd'*40
    register(app.state.store, release, key, [{'index': 0, 'name': 'Fixture.S01E01.avi'}, {'index': 1, 'name': 'Fixture.S01E02.avi'}])
    url = '/v1/history/'+release['movie']
    assert client.put(url, headers=auth, json={'position': 12000, 'torrent': key, 'file': 0, 'episode': 's001e0001'}).status_code == 200
    assert client.put(url, headers=auth, json={'position': 0, 'torrent': key, 'file': 1, 'episode': 's001e0002', 'completed': True, 'duration': 60000}).status_code == 200
    movie = client.get('/v1/movies/'+release['movie'], headers=auth).json()
    assert movie['episodes'][0]['position'] == 12000
    assert movie['episodes'][1]['completed']
    assert all(e['position'] == 0 and not e['completed'] for e in client.get('/v1/movies/'+release['movie'], headers=other).json()['episodes'])
    assert client.put(url, headers=auth, json={'position': 1, 'torrent': key, 'file': 0, 'episode': 's001e0002'}).status_code == 400


def test_buffer_requires_entire_index_window(client, monkeypatch):
    from types import SimpleNamespace as NS
    manager = app.state.torrents
    available = set(range(15)) | {63}
    status = NS(progress=.25, download_payload_rate=1024, num_seeds=1, num_peers=1, has_metadata=True, errc=NS(value=lambda: 0))
    handle = NS(status=lambda: status, have_piece=lambda p: p in available, set_piece_deadline=lambda *_: None)
    info = NS(piece_length=lambda: 1024*1024, piece_size=lambda _: 1024*1024)
    manager.handles['index-test'] = handle
    monkeypatch.setattr(manager, 'files', lambda _: [])
    monkeypatch.setattr(manager, 'file', lambda *_: (handle, info, None, 64*1024*1024, 0))
    try:
        assert not manager.status('index-test')['ready']
        available.update(range(56, 64))
        assert not manager.status('index-test')['ready']
        manager.media_info[('index-test', 0)] = {'duration_ms': 128000}
        assert manager.status('index-test')['ready']
    finally:
        del manager.handles['index-test']


def test_start_existing_torrent_reuses_handle_without_tracker(client, monkeypatch):
    auth=login(client,'reuse-torrent')
    release=normalize('local',{'Id':'98','Title':'Reuse (2026)'})
    release['torrent_id']='reuse'
    app.state.catalog.put(release)
    app.state.torrents.handles['reuse']=object()
    async def unavailable(_):
        raise AssertionError('Existing torrent must not contact tracker')
    monkeypatch.setattr(app.state.catalog,'detail',unavailable)
    try:
        assert client.post('/v1/torrents',headers=auth,json={'release':'local:98'}).json()=={'id':'reuse'}
    finally: del app.state.torrents.handles['reuse']


def test_missing_source_returns_actionable_error(client, monkeypatch):
    auth=login(client,'missing-source')
    async def detail(_): return normalize('local',{'Id':'99','Title':'Unavailable (2026)'})
    monkeypatch.setattr(app.state.catalog,'detail',detail)
    response=client.post('/v1/torrents',headers=auth,json={'release':'local:99'})
    assert response.status_code==409
    assert response.json()['detail']['code']=='source_missing'


def test_disc_folder_is_not_reported_as_waiting_for_metadata(client, monkeypatch):
    auth=login(client,'disc-folder')
    monkeypatch.setattr(app.state.torrents,'status',lambda *_: {'metadata_ready':True,'files':[],'error':None})
    response=client.get('/v1/torrents/disc',headers=auth)
    assert response.status_code==200
    assert 'Blu-ray' in response.json()['error']


def test_verified_range_limits_and_unavailable_data(client, monkeypatch):
    auth = login(client, 'verified')
    monkeypatch.setattr(app.state.torrents, 'file', lambda *_: (None, None, None, 8*1024*1024, 0))
    async def unavailable(*args):
        raise TimeoutError('Unavailable')
        yield b''
    monkeypatch.setattr(app.state.torrents, 'stream', unavailable)
    path = '/v1/torrents/test/files/0'
    assert client.get(path, headers=auth | {'Range': 'bytes=0-4194304', 'X-MCore-Integrity': 'sha256'}).status_code == 400
    assert client.get(path, headers=auth | {'Range': 'bytes=0-1023', 'X-MCore-Integrity': 'sha256'}).status_code == 503


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
        return normalize('local', {'Id': '1', 'Title': 'Sample (2026)', 'Magnet': lt.make_magnet_uri(info)})
    monkeypatch.setattr(app.state.catalog, 'detail', detail)
    try:
        app.state.torrents.config['cache_bytes'] = 1048576
        response = client.post('/v1/torrents', headers=auth, json={'release': 'local:1'})
        assert response.status_code == 200, response.text
        key = response.json()['id']
        download = app.state.torrents.handles[key]
        deadline = time.monotonic()+30
        while not seed.is_listening() and time.monotonic()<deadline:
            time.sleep(.05)
        download.connect_peer(('127.0.0.1', seed.listen_port()))
        while key not in app.state.torrents.errors and time.monotonic()<deadline:
            time.sleep(.1)
        assert 'Cache capacity' in app.state.torrents.errors.get(key, '')
        assert download.status().total_done == 0, 'Payload must wait for cache admission'
        app.state.torrents.config['cache_bytes'] = 104857600
        while not download.status().is_seeding and time.monotonic()<deadline:
            time.sleep(.1)
        assert download.status().is_seeding, str(download.status().errc)
        from mcore.telemetry import snapshot
        observed = next(r for r in snapshot(app.state.torrents)['torrents'] if r['id'] == key)
        assert observed['name'] == 'sample.mkv'
        assert observed['percent'] == 100 and observed['size'] == len(payload)
        assert observed['download_rate'] >= 0 and observed['upload_rate'] >= 0
        assert observed['uploaded'] >= 0
        path = f'/v1/torrents/{key}/files/0'
        status = client.get(f'/v1/torrents/{key}', headers=auth)
        assert status.status_code == 200, status.text
        assert not status.json()['ready'], 'Verified bytes alone do not make random data playable'
        assert status.json()['buffer_missing_bytes'] == 0
        assert client.get(path).status_code == 401
        head = client.head(path, headers=auth)
        assert int(head.headers['Content-Length']) == len(payload)
        response = client.get(path, headers=auth | {'Range': 'bytes=16380-16400'})
        assert response.status_code == 206
        assert response.content == payload[16380:16401]
        response = client.get(path, headers=auth | {'Range': 'bytes=-100'})
        assert response.content == payload[-100:]
        verified = client.get(path, headers=auth | {'Range': 'bytes=16380-16400', 'X-MCore-Integrity': 'sha256'})
        assert verified.status_code == 206
        assert verified.headers['content-range'] == f'bytes 16380-16400/{len(payload)}'
        assert verified.content == payload[16380:16401]
        assert verified.headers['x-mcore-sha256'] == hashlib.sha256(verified.content).hexdigest()
        assert client.get(path, headers=auth | {'X-MCore-Integrity': 'sha256'}).status_code == 400
        assert client.get(path, headers=auth | {'Range': 'bytes=0-10', 'X-MCore-Integrity': 'crc32'}).status_code == 400
        tail = client.get(path, headers=auth | {'Range': 'bytes=-100', 'X-MCore-Integrity': 'sha256'})
        assert tail.headers['x-mcore-sha256'] == hashlib.sha256(payload[-100:]).hexdigest()
        assert client.get(path, headers=auth | {'Range': f'bytes={len(payload)}-'}).status_code == 416
        whole = client.get(path, headers=auth)
        assert hashlib.sha256(whole.content).digest() == hashlib.sha256(payload).digest()
    finally:
        seed.pause()
