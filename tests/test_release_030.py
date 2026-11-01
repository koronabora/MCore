import asyncio
import json
import math
import subprocess
from types import SimpleNamespace as NS
import httpx
from mcore.store import Store
from mcore.catalog import Catalog, normalize
from mcore.episodes import identify, register, listing
from mcore.inspection import Inspection
from mcore.archive import parse
from mcore.sync import Synchronizer


def test_episode_identity_and_account_isolation(tmp_path):
    release = normalize('local', {'Id': '1', 'Title': 'Example S02 2025 1080p'})
    assert identify('Example.S02E03.mkv', release) == ('s002e0003', 2, 3)
    assert identify('Example 2x03.avi', release) == ('s002e0003', 2, 3)
    assert identify('Season 2/03 - Name.mkv', release) == ('s002e0003', 2, 3)
    assert identify('Extras.mkv', release)[0] == ''
    assert identify('Example.S02E03-E04.mkv', release)[0] == ''
    store = Store(tmp_path)
    register(store, release, 'a'*40, [{'index': 0, 'name': 'Example.S02E03.mkv'}])
    register(store, dict(release, id='local:2'), 'b'*40, [{'index': 1, 'name': 'Example.2x03.avi'}])
    with store.connect() as db:
        db.execute('INSERT INTO episode_history VALUES (?,?,?,?,?,?,?,?,?)', ('user', release['movie'], 's002e0003', 12345, 900000, 0, 'a'*40, 0, 1))
    result = listing(store, [release['movie']], 'user')
    assert len(result) == 1 and len(result[0]['sources']) == 2 and result[0]['position'] == 12345
    assert listing(store, [release['movie']], 'other')[0]['position'] == 0


def test_archive_rows_and_cursor_are_not_comments_or_navigation():
    html = '''<h4 class="forumlink"><a href="viewforum.php?f=12">Фильмы</a></h4>
    <a href="viewtopic.php?t=9">Rules</a><tr><td><a class="torTopic" href="viewtopic.php?t=42">Фильм / Movie [2024, BDRip]</a>
    <span class="seedmed">12</span><span class="leechmed">2</span><a class="f-dl">1 GB</a></td></tr>
    <a href="viewforum.php?f=7&amp;start=50">Next</a><a href="viewforum.php?f=8&amp;start=50">Other</a>'''
    rows, links = parse('rutracker', html, '/forum/viewforum.php?f=7')
    assert len(rows) == 1 and rows[0]['Id'] == '42' and rows[0]['Seeds'] == '12'
    assert links == ['/forum/viewforum.php?f=12', '/forum/viewforum.php?f=7&start=50']
    rows, links = parse('kinozal', '''<tr><td></td><td class="nam"><a href="/details.php?id=4">Movie (2024)</a></td><td></td><td>2 GB</td><td>10</td><td>1</td></tr><a href="browse.php?c=1002&amp;page=1">Next</a>''', '/browse.php?c=1002&page=0')
    assert rows[0]['Id'] == '4' and links == ['/browse.php?c=1002&page=1']


def test_sync_retry_and_completed_cursor_survive_restart(tmp_path):
    async def check():
        config = {'providers': ['kinozal'], 'torapi_url': 'http://localhost:1', 'sync_seconds': 21600}
        catalog = Catalog(Store(tmp_path), config)
        sync = Synchronizer(catalog)
        with catalog.store.connect() as db:
            sync.enqueue(db, 'search', 'kinozal', {'query': 'Test'}, 0)
        async def unavailable(*_):
            return 'kinozal'
        catalog.provider_search = unavailable
        try:
            assert await sync.step()
            restored = Synchronizer(catalog)
            assert restored.progress()['jobs'][0]['retrying'] == 1
            assert not await restored.step()
            async def available(*_):
                return None
            catalog.provider_search = available
            with catalog.store.connect() as db:
                db.execute('UPDATE sync_jobs SET due=0')
            assert await restored.step()
            with catalog.store.connect() as db:
                row = db.execute('SELECT * FROM sync_jobs').fetchone()
                assert row['completed'] > 0 and row['attempts'] == 0 and row['due'] > row['completed']
        finally:
            await catalog.close()
    asyncio.run(check())


def test_external_identity_unifies_translations_but_keeps_remakes(tmp_path):
    async def check():
        catalog = Catalog(Store(tmp_path), {'providers': ['kinozal', 'rutracker'], 'torapi_url': 'http://localhost:1'})
        try:
            one = normalize('kinozal', {'Id': '1', 'Title': 'Название / Original (2000) 1080p'})
            two = normalize('rutracker', {'Id': '2', 'Title': 'Другое название / Alternate (2000) 720p'})
            remake = normalize('kinozal', {'Id': '3', 'Title': 'Название / Original (2026) 1080p'})
            one['external_ids'] = two['external_ids'] = {'imdb': 'tt12345'}
            remake['external_ids'] = {'imdb': 'tt67890'}
            catalog.put(one); catalog.put(two); catalog.put(remake)
            assert catalog.release(one['id'])['movie'] == catalog.release(two['id'])['movie']
            assert catalog.release(one['id'])['movie'] != catalog.release(remake['id'])['movie']
            assert normalize('kinozal', {'Id': '5', 'Title': 'Сериал (2 сезон: 1-8 серии) / Original / 2025 / WEB-DL'})['season'] == '2'
        finally:
            await catalog.close()
    asyncio.run(check())


def test_probe_reads_verified_parts_of_incomplete_avi_and_persists(tmp_path):
    async def check():
        video = tmp_path/'partial.avi'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=24', '-t', '10', '-c:v', 'mpeg4', '-q:v', '2', str(video)], check=True)
        size = video.stat().st_size; piece_size = 262144
        assert size > 8*1024*1024
        have = set(range(16)) | set(range((size-2*1024*1024)//piece_size, math.ceil(size/piece_size)))
        wanted = []
        handle = NS(have_piece=lambda p: p in have, set_piece_deadline=lambda p, _: wanted.append(p))
        info = NS(piece_length=lambda: piece_size, num_pieces=lambda: math.ceil(size/piece_size))
        torrents = NS(store=Store(tmp_path), active={}, last_used={}, file=lambda *_: (handle, info, video, size, 0))
        inspector = Inspection(torrents, tmp_path)
        data = await inspector.metadata('fixture', 0)
        assert 9900 <= data['duration_ms'] <= 10100 and data['average_bitrate'] > 0
        assert data['streams'][0]['codec_type'] == 'video'
        async with inspector.source('fixture', 0) as url:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers={'Range': f'bytes={5*1024*1024}-{5*1024*1024+100}'})
                assert response.status_code == 503 and not response.content
        assert wanted and len(have) < info.num_pieces()
        restored = Inspection(torrents, tmp_path)
        assert restored.info[('fixture', 0)]['duration_ms'] == data['duration_ms']
        assert torrents.active['fixture'] == 0
    asyncio.run(check())
