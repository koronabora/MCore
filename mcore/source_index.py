"""Durable metadata-only indexing. No payload is requested by this worker."""
import asyncio
import time
from . import episodes


class SourceIndex:
    def __init__(self, store, catalog, torrents, start):
        self.store, self.catalog, self.torrents, self.start = store, catalog, torrents, start

    def schedule(self, releases):
        with self.store.connect() as db:
            for release in releases:
                db.execute('INSERT OR IGNORE INTO source_index_jobs(release,due) VALUES (?,?)', (release['id'], time.time()))

    async def run(self):
        while True:
            with self.store.connect() as db:
                rows = db.execute('SELECT * FROM source_index_jobs WHERE completed=0 AND attempts<3 AND due<=? ORDER BY due LIMIT 2', (time.time(),)).fetchall()
            if not rows:
                await asyncio.sleep(2)
                continue
            await asyncio.gather(*(self.index(dict(row)) for row in rows))

    async def index(self, job):
        rid = job['release']
        try:
            key = await self.start(rid)
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
                           (time.time()+300, str(error)[:200] or type(error).__name__, rid))
