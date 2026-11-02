import hashlib
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from mcore.api import app, user
from mcore import updates


def test_update_publication_auth_and_immutable_download(tmp_path, monkeypatch):
    monkeypatch.setattr(app.state, 'catalog', SimpleNamespace(config={'state_dir': str(tmp_path)}), raising=False)
    monkeypatch.setattr(app.state, 'store', SimpleNamespace(authenticate=lambda key: 'test' if key == 'valid' else None), raising=False)
    client = TestClient(app)
    auth = {'Authorization': 'Bearer valid'}
    assert client.get('/v1/app-update').status_code == 401
    assert client.get('/v1/app-update', headers=auth).json() == {'version_code': 0}
    assert client.get('/').status_code == 404
    root = updates.directory(tmp_path)
    root.mkdir()
    payload = b'fixture-apk-bytes'
    digest = hashlib.sha256(payload).hexdigest()
    release = dict(version_code=21, version_name='0.5', sha256=digest, size=len(payload), notes='Test')
    (root/'latest.json').write_text(json.dumps(release))
    assert client.get('/v1/app-update', headers=auth).status_code == 503
    (root/(digest+'.apk')).write_bytes(payload)
    assert client.get('/v1/app-update', headers=auth).json() == release
    public = client.get('/')
    assert public.status_code == 200 and public.content == payload
    assert 'MApp-0.5.apk' in public.headers['content-disposition']
    assert public.headers['cache-control'] == 'no-store'
    assert client.get('/v1/app-update/'+digest+'.apk').status_code == 401
    response = client.get('/v1/app-update/'+digest+'.apk', headers=auth)
    assert response.content == payload and response.status_code == 200
    assert client.get('/v1/app-update/not-a-hash.apk', headers=auth).status_code == 404
    newer = b'newer-fixture-apk'
    newer_hash = hashlib.sha256(newer).hexdigest()
    (root/(newer_hash+'.apk')).write_bytes(newer)
    release.update(version_code=22, version_name='0.5.1', sha256=newer_hash, size=len(newer))
    (root/'latest.json').write_text(json.dumps(release))
    public = client.get('/')
    assert public.content == newer and 'MApp-0.5.1.apk' in public.headers['content-disposition']
    assert client.get('/v1/app-update/'+digest+'.apk', headers=auth).content == payload
    release['sha256'] = '../secret'
    (root/'latest.json').write_text(json.dumps(release))
    assert client.get('/v1/app-update', headers=auth).status_code == 503
