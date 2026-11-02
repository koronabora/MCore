import asyncio
import json
import time
from types import SimpleNamespace
from mcore.catalog import Catalog, normalize
from mcore.store import Store
from mcore.source_index import SourceIndex
from mcore.sync import Synchronizer
from mcore import episodes, archive


def test_catalog_discovery_indexes_unopened_releases_and_search_survives_restart(tmp_path):
    async def check():
        config = {'providers':['kinozal'],'torapi_url':'http://localhost:1','sync_seconds':21600}
        store = Store(tmp_path)
        catalog = Catalog(store,config)
        item = normalize('kinozal',dict(Id='1',Title='Новый сериал / New Series S01 2026 720p',Size='1 GiB'))
        catalog.put(item)
        with store.connect() as db:
            assert db.execute('SELECT count(*) FROM source_index_jobs').fetchone()[0] == 1
        episodes.register(store,item,'torrent',[dict(index=0,name='S01E01.mkv',size=100)])
        with store.connect() as db:
            db.execute('UPDATE source_index_jobs SET completed=1')
        catalog.put(dict(item,seeds=123))
        with store.connect() as db:
            assert db.execute('SELECT completed FROM source_index_jobs').fetchone()[0] == 1
        assert catalog.movies('New Series')[0]['id'] == item['movie']
        catalog.put(dict(item,size='2 GiB'))
        with store.connect() as db:
            assert db.execute('SELECT completed FROM source_index_jobs').fetchone()[0] == 0
        await catalog.close()
        restarted = Catalog(store,config)
        assert restarted.movies('Новый сериал')[0]['id'] == item['movie']
        assert episodes.catalog(store,[item],[item['movie']],'test')['works']
        await restarted.close()
    asyncio.run(check())


def test_exhausted_sources_are_retried_with_bounded_backoff(tmp_path):
    async def check():
        store=Store(tmp_path)
        with store.connect() as db:
            db.execute("INSERT INTO source_index_jobs(release,due,attempts) VALUES ('retry',0,3)")
        ran=asyncio.Event()
        async def start(rid):
            ran.set()
            raise TimeoutError('offline')
        index=SourceIndex(store,None,None,start,concurrency=1)
        task=asyncio.create_task(index.run())
        try:
            await asyncio.wait_for(ran.wait(),1)
            await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        with store.connect() as db:
            row=db.execute('SELECT * FROM source_index_jobs').fetchone()
            assert row['attempts']==4 and time.time()+2000 < row['due'] < time.time()+86401
    asyncio.run(check())


def test_source_revision_changes_for_files_not_peer_counts(tmp_path):
    store=Store(tmp_path)
    item=normalize('local',dict(Id='1',Title='Fixture S01'))
    args=([item],[item['movie']],'u')
    before=episodes.version(store,*args)
    item.update(seeds=999,checked_at=time.time())
    assert episodes.version(store,*args)==before
    episodes.register(store,item,'key',[dict(index=0,name='S01E01.mkv',size=1)])
    assert episodes.version(store,*args)!=before


def test_video_roots_include_sport_education_music_and_all_page_links():
    html=''.join(f'<div id="c-{category}"><div class="forumlink"><a href="viewforum.php?f={category}">Video</a></div></div>' for category in (2,18,20,28,10,39,8))
    rows,links=archive.parse('rutracker',html,'/forum/index.php')
    assert set(links)=={f'/forum/viewforum.php?f={n}' for n in (2,18,20,28,10,39)}
    html='<div id="forum-title">Video</div><a href="viewforum.php?f=28&start=50">2</a><a href="viewforum.php?f=28&start=100">3</a>'
    assert archive.parse('rutracker',html,'/forum/viewforum.php?f=28')[1]==['/forum/viewforum.php?f=28&start=100','/forum/viewforum.php?f=28&start=50']


def test_archive_progress_and_first_pass_priority(tmp_path, monkeypatch):
    async def check():
        catalog=Catalog(Store(tmp_path),{'providers':['kinozal'],'torapi_url':'http://localhost:1','sync_seconds':21600})
        sync=Synchronizer(catalog)
        catalog.metadata=object()
        sync.seed()
        with catalog.store.connect() as db:
            jobs=db.execute("SELECT id FROM sync_jobs WHERE kind='archive' ORDER BY id").fetchall()
            db.execute('UPDATE sync_jobs SET completed=1,due=0 WHERE id=?',(jobs[0]['id'],))
        progress=sync.progress()
        assert len(progress['sections'])==5
        assert sum(s['pages_visited'] for s in progress['sections'])==1
        assert sum(s['pending_first_pass'] for s in progress['sections'])==4
        visited=[]
        async def page(metadata,provider,path):
            visited.append(path)
            return [],[]
        monkeypatch.setattr(archive,'page',page)
        await sync.step()
        with catalog.store.connect() as db:
            old=json.loads(db.execute('SELECT payload FROM sync_jobs WHERE id=?',(jobs[0]['id'],)).fetchone()[0])['path']
        assert visited and old not in visited
        await catalog.close()
    asyncio.run(check())
