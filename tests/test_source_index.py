import asyncio
from types import SimpleNamespace

from mcore.source_index import SourceIndex
from mcore.store import Store
from mcore.episodes import identify


def test_breaking_bad_episode_of_total_does_not_invent_seasons():
    release = dict(series=True, season='2')
    name = "Breaking Bad ~ 2nd season/Breaking.Bad•2nd.season•ep.-06x13~'Peekaboo'.(BDRip).avi"
    assert identify(name, release) == ('s002e0006', 2, 6)
    assert identify('Breaking.Bad.2x06.mkv', release) == ('s002e0006', 2, 6)
    assert identify('Breaking.Bad.S02E06.mkv', release) == ('s002e0006', 2, 6)


def test_free_slot_refilled_without_waiting_for_slow_release_and_cancel_cleanup(tmp_path):
    async def check():
        store = Store(tmp_path)
        started, cancelled = [], []
        third = asyncio.Event()
        async def start(rid):
            started.append(rid)
            if rid == 'fast':
                return rid
            if rid == 'third':
                third.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(rid)
        releases = {rid: dict(id=rid, movie='m', series=False) for rid in ('slow', 'fast', 'third')}
        index = SourceIndex(store, SimpleNamespace(release=releases.get),
                            SimpleNamespace(files=lambda key: [dict(index=0, name='movie.mkv', size=1)]), start, concurrency=2)
        index.schedule(list(releases.values()))
        task = asyncio.create_task(index.run())
        try:
            await asyncio.wait_for(third.wait(), 2)
            assert started == ['slow', 'fast', 'third']
            with store.connect() as db:
                assert db.execute("SELECT completed FROM source_index_jobs WHERE release='fast'").fetchone()[0] > 0
                assert db.execute('SELECT count(*) FROM source_files').fetchone()[0] == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert set(cancelled) == {'slow', 'third'}
        assert len(started) == len(set(started))
    asyncio.run(check())


def test_open_card_prioritized_by_seeds_without_resetting_retry_backoff(tmp_path):
    async def check():
        store = Store(tmp_path)
        started = []
        ready = asyncio.Event()
        async def start(rid):
            started.append(rid)
            ready.set()
            await asyncio.Event().wait()
        index = SourceIndex(store, None, None, start, concurrency=1)
        with store.connect() as db:
            db.execute("INSERT INTO source_index_jobs(release,due) VALUES ('old',0)")
            db.execute("INSERT INTO source_index_jobs(release,due,attempts) VALUES ('retry',9999999999,1)")
        index.schedule([dict(id='low', seeds=1), dict(id='high', seeds=100), dict(id='retry', seeds=999)])
        task = asyncio.create_task(index.run())
        try:
            await asyncio.wait_for(ready.wait(), 2)
            assert started == ['high']
            with store.connect() as db:
                row = db.execute("SELECT * FROM source_index_jobs WHERE release='retry'").fetchone()
                assert row['attempts'] == 1 and row['due'] == 9999999999
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


def test_failed_source_does_not_block_next_source(tmp_path):
    async def check():
        store = Store(tmp_path)
        next_started = asyncio.Event()
        async def start(rid):
            if rid == 'bad':
                raise TimeoutError('tracker timeout')
            next_started.set()
            await asyncio.Event().wait()
        index = SourceIndex(store, None, None, start, concurrency=1)
        index.schedule([dict(id='bad'), dict(id='good')])
        task = asyncio.create_task(index.run())
        try:
            await asyncio.wait_for(next_started.wait(), 2)
            with store.connect() as db:
                row = db.execute("SELECT * FROM source_index_jobs WHERE release='bad'").fetchone()
                assert row['attempts'] == 1 and row['error'] == 'tracker timeout'
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())
