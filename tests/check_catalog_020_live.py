"""Opt-in checks of the installed catalog against the user's two configured trackers."""
import ssl
import httpx
from mcore.config import load
from mcore.store import Store, digest

config = load()
store = Store(config['state_dir'])
with httpx.Client(base_url=config['public_url'], verify=ssl.create_default_context(cafile=config['tls_cert']), timeout=180) as client:
    key = client.post('/v1/pair', json={'token': store.invite('emulator-test')}).json()['key']
    client.headers['Authorization'] = 'Bearer ' + key
    try:
        for query in ('Большой куш', 'One Piece', 'Интерстеллар'):
            response = client.get('/v1/search', params={'q': query})
            response.raise_for_status()
            movies = response.json()['movies']
            print('SEARCH', query, 'cards', len(movies), 'first', [(m['title'], m['exact'], len(m['releases'])) for m in movies[:4]], flush=True)
            assert not any(r['resolution'] == 'Original' for m in movies for r in m['releases'])
            if query == 'Интерстеллар':
                movie = client.get('/v1/movies/' + movies[0]['id'])
                movie.raise_for_status()
                assert len(movie.json()['description']) > 40
                poster = client.get('/v1/movies/' + movies[0]['id'] + '/poster')
                poster.raise_for_status()
                print('METADATA description', len(movie.json()['description']), 'poster bytes', len(poster.content), flush=True)
    finally:
        with store.connect() as db:
            db.execute('DELETE FROM sessions WHERE hash=?', (digest(key),))
