"""Durable metadata-only indexing. No payload is requested by this worker."""
import asyncio
import time
from . import episodes


class SourceIndex:
    def __init__(self, store, catalog, torrents, start, concurrency=8):
        self.store, self.catalog, self.torrents, self.start = store, catalog, torrents, start
        self.concurrency = concurrency
        self.requested = {}

    def schedule(self, releases):
        now = time.time()
        self.requested = {rid: value for rid, value in self.requested.items() if value[0] > now-300}
        with self.store.connect() as db:
            for release in releases:
                self.requested[release['id']] = (now, release.get('seeds', 0) or 0)
                db.execute('INSERT OR IGNORE INTO source_index_jobs(release,due) VALUES (?,?)', (release['id'], now))

    async def run(self):
        active = {}
        try:
            while True:
                now = time.time()
                with self.store.connect() as db:
                    candidates = {r['release']: r for r in db.execute('SELECT * FROM source_index_jobs WHERE completed=0 AND due<=? ORDER BY due LIMIT 128', (now,))}
                    requested = [rid for rid, value in self.requested.items() if value[0] > now-300]
                    if requested:
                        for row in db.execute('SELECT * FROM source_index_jobs WHERE completed=0 AND due<=? AND release IN ('+','.join('?' for _ in requested)+')', (now,*requested)):
                            candidates[row['release']] = row
                    rows = list(candidates.values())
                def priority(row):
                    requested, seeds = self.requested.get(row['release'], (0, 0))
                    return (requested <= now-300, -seeds if requested > now-300 else 0, row['due'])
                for row in sorted(rows, key=priority):
                    if len(active) >= self.concurrency:
                        break
                    if row['release'] not in active:
                        active[row['release']] = asyncio.create_task(self.index(dict(row)))
                if active:
                    # Refill each free slot immediately; one slow peer must not hold a batch.
                    done, _ = await asyncio.wait(active.values(), timeout=1, return_when=asyncio.FIRST_COMPLETED)
                    for rid, task in list(active.items()):
                        if task in done:
                            del active[rid]
                            task.result()
                else:
                    await asyncio.sleep(1)
        finally:
            for task in active.values():
                task.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)

    async def index(self, job):
        rid = job['release']
        key = None
        try:
            # Include tracker/download waits in a bounded per-release budget.
            key = await asyncio.wait_for(self.start(rid), timeout=90)
            deadline = time.monotonic()+60
            while time.monotonic() < deadline:
                files = self.torrents.files(key)
                if files:
                    episodes.register(self.store, self.catalog.release(rid), key, files)
                    with self.store.connect() as db:
                        db.execute('UPDATE source_index_jobs SET completed=?,error=? WHERE release=?', (time.time(), '', rid))
                    return
                await asyncio.sleep(1)
            raise TimeoutError('Метаданные пока недоступны от пиров')
        except Exception as error:
            with self.store.connect() as db:
                db.execute('UPDATE source_index_jobs SET attempts=attempts+1,due=?,error=? WHERE release=?',
                           (time.time()+min(86400, 300*2**min(job['attempts'],9)), str(error)[:200] or type(error).__name__, rid))
        finally:
            if key and hasattr(self.torrents, 'release_metadata'):
                self.torrents.release_metadata(key)
