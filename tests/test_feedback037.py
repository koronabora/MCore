import json
import pytest
from mcore import episodes
from mcore.store import Store


def test_cached_documentary_episodes_share_sources(tmp_path):
    store = Store(tmp_path)
    releases = [dict(id=r, movie='wild-west', series=False, season='') for r in ('sd', '720', '1080')]
    with store.connect() as db:
        for r in releases:
            for n, name in enumerate(('Gold.Rush', 'True.Grit', 'High.Noon')):
                file = dict(index=n, name=f'NG.The.Wild.West/Episode.{n+1}.{name}.mkv', size=100,
                            episode=f"file:{r['id']}:{n}", season=None, number=None, torrent=r['id'], release=r['id'])
                db.execute('INSERT INTO source_files VALUES (?,?,?)', (r['id'], n, json.dumps(file)))
    result = episodes.catalog(store, releases, ['wild-west'], 'u')
    assert all(r['series'] for r in releases)
    assert [w['number'] for w in result['works']] == [1, 2, 3]
    assert all(w['season'] == 0 and len(w['sources']) == 3 for w in result['works'])
    assert all(not value for value in result['complete_seasons'].values())


@pytest.mark.parametrize('missing,gap,declared,expected', [(False, False, 65, True),
    (True, False, 65, False), (False, True, 65, False), (False, False, 66, False)])
def test_multiseason_pack_requires_all_declared_episodes(tmp_path, missing, gap, declared, expected):
    store = Store(tmp_path)
    release = dict(id='spider', movie='m', series=True, season='1-5',
                   release_title=f'Сезон: 1-5 / Серии: 1-{declared} из {declared}')
    files = []
    for season, count in enumerate((13, 14, 14, 11, 13), 1):
        for n in range(1, count+1):
            if missing and season == 3 and n == 14:
                continue
            number = 15 if gap and season == 3 and n == 14 else n
            files.append(dict(index=len(files), name=f'Spider-Man {season}x{number:02}.mkv', size=100))
    files.append(dict(index=len(files), name='Spider-Man 2x09 Uncut.mkv', size=100))
    episodes.register(store, release, 't', files)
    result = episodes.catalog(store, [release], ['m'], 'u')
    assert result['complete_seasons']['spider'] == ([1, 2, 3, 4, 5] if expected else [])


def test_plain_movie_sources_remain_one_movie(tmp_path):
    store = Store(tmp_path)
    releases = [dict(id=r, movie='m', series=False, season='') for r in ('a', 'b')]
    for r in releases:
        episodes.register(store, r, r['id'], [dict(index=0, name='Movie.2013.mkv', size=100)])
    works = episodes.catalog(store, releases, ['m'], 'u')['works']
    assert not any(r['series'] for r in releases)
    assert len(works) == 1 and len(works[0]['sources']) == 2
