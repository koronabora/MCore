import asyncio
import hashlib
from types import SimpleNamespace
import pytest
from mcore.store import Store
from mcore import episodes
from mcore.torrents import Torrents
from mcore.images import validate_poster
from PIL import Image
from io import BytesIO


def test_canonical_episode_sources_and_complete_season(tmp_path):
    store = Store(tmp_path)
    a = dict(id='a', movie='m', series=True, season='1', release_title='Сезон 1 Серии: 1-2 из 2')
    b = dict(a, id='b')
    episodes.register(store, a, 'ta', [dict(index=n, name=f'S01E{n+1:02}.mkv', size=100) for n in range(2)])
    episodes.register(store, b, 'tb', [dict(index=0, name='S01E02.mp4', size=50)])
    result = episodes.catalog(store, [a, b], ['m'], 'u')
    assert len(result['works']) == 2
    assert len(result['works'][1]['sources']) == 2
    assert result['complete_seasons'] == {'a': [1], 'b': []}
    assert result['works'][1]['sources'][1]['size'] == 50
    # A partial manifest alone must never prove season completeness.
    b['release_title'] = 'Сезон 1'
    assert episodes.catalog(store, [b], ['m'], 'u')['complete_seasons']['b'] == []


def test_ambiguous_files_stay_source_specific(tmp_path):
    store = Store(tmp_path)
    releases = [dict(id=k, movie='m', series=True, season='1-5') for k in ('a', 'b')]
    for r in releases:
        episodes.register(store, r, r['id'], [dict(index=0, name='01.mkv', size=1)])
    assert len(episodes.catalog(store, releases, ['m'], 'u')['works']) == 2


def test_small_or_wide_captcha_poster_rejected():
    for size in ((120, 40), (300, 100), (600, 900)):
        stream = BytesIO(); Image.new('RGB', size).save(stream, format='PNG')
        if size == (600, 900): validate_poster(stream.getvalue())
        else:
            with pytest.raises(ValueError): validate_poster(stream.getvalue())


def test_verified_disk_piece_rejects_corruption_and_shares_read():
    async def run():
        torrents = Torrents.__new__(Torrents)
        torrents.piece_reads = {}; torrents.piece_waiters = {}
        good = b'original torrent piece'
        info = SimpleNamespace(piece_size=lambda p: len(good), info_hashes=lambda: SimpleNamespace(has_v1=lambda: True), hash_for_piece=lambda p: hashlib.sha1(good).digest())
        state = {'data': good, 'reads': 0, 'rechecks': 0}
        def read(piece):
            state['reads'] += 1
            asyncio.get_running_loop().call_soon(torrents.piece_reads[('t', piece)].set_result, state['data'])
        def recheck(): state['rechecks'] += 1
        torrents.handles = {'t': SimpleNamespace(read_piece=read, torrent_file=lambda: info, force_recheck=recheck)}
        assert await asyncio.gather(torrents.verified_piece('t', 0), torrents.verified_piece('t', 0)) == [good, good]
        assert state['reads'] == 1 and not torrents.piece_reads
        state['data'] = b'x'*len(good)
        with pytest.raises(IOError, match='checksum mismatch'): await torrents.verified_piece('t', 0)
        assert state['rechecks'] == 1 and not torrents.piece_reads
    asyncio.run(run())


def test_imdb_import_only_explicit_parent_ids_and_no_unknown_counts(tmp_path):
    from mcore.imdb_import import import_episodes
    store = Store(tmp_path)
    import json
    with store.connect() as db:
        db.execute('INSERT INTO releases VALUES (?,?,?,?)', ('a', 'movie', json.dumps(dict(series=True, movie='movie', external_ids={'imdb': 'tt1'})), 0))
    path = tmp_path/'episodes.tsv'
    path.write_text('tconst\tparentTconst\tseasonNumber\tepisodeNumber\n'
                    'tt11\ttt1\t1\t1\n'
                    'tt12\ttt1\t1\t2\n'
                    'tt21\ttt1\t2\t1\n'
                    'tt22\ttt1\t2\t\\N\n'
                    'tt99\ttt9\t1\t1\n')
    assert import_episodes(store, path) == 1
    with store.connect() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM season_metadata')]
    assert rows == [dict(movie='movie', season=1, total=2, origin='imdb:tt1')]


def test_cycle_parts_use_explicit_film_and_part_numbers():
    assert episodes.identify('Фильм 2/Серия 1.mkv', {'cycle': True}) == ('film002part001', 2, 1)
    assert episodes.identify('Шерлок Холмс.mkv', {'cycle': True}) == ('', None, None)


def test_metadata_index_does_not_reserve_payload_or_evict_seed(tmp_path, monkeypatch):
    store = Store(tmp_path)
    with store.connect() as db:
        db.execute('INSERT INTO torrents VALUES (?,?,?,?)', ('seed', 'magnet:', 1, 2*1024*1024))
        db.execute('INSERT INTO torrents VALUES (?,?,?,?)', ('metadata', 'magnet:', 2, 0))
    manager = Torrents.__new__(Torrents)
    manager.store=store; manager.config={'cache_bytes': 3*1024*1024}; manager.root=tmp_path; manager.resume=tmp_path
    manager.metadata_only={'metadata'}; manager.active={}; manager.last_used={}; manager.errors={}; manager.lock=asyncio.Lock()
    state = {'evicted': False, 'upload_only': False}
    info=SimpleNamespace(total_size=lambda: 2*1024*1024)
    seed=SimpleNamespace(torrent_file=lambda: info, status=lambda: SimpleNamespace(is_seeding=True, paused=False), set_upload_mode=lambda value: None, need_save_resume_data=lambda: False)
    meta=SimpleNamespace(torrent_file=lambda: info, set_upload_mode=lambda value: state.update(upload_only=value), need_save_resume_data=lambda: False)
    manager.handles={'seed': seed, 'metadata': meta}
    manager.session=SimpleNamespace(remove_torrent=lambda *args: state.update(evicted=True))
    async def stop_after_iteration(_): raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, 'sleep', stop_after_iteration)
    with pytest.raises(asyncio.CancelledError): asyncio.run(manager.maintain())
    assert state == {'evicted': False, 'upload_only': True}
    with store.connect() as db: assert db.execute('SELECT size FROM torrents WHERE id="metadata"').fetchone()['size'] == 0
