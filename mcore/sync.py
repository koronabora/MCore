"""Durable fair work queue: page cursors, retry deadlines and progress survive restart."""
import asyncio
import hashlib
import json
import time
import httpx
from . import archive


class Synchronizer:
    def __init__(self, catalog):
        self.catalog = catalog
        self.store = catalog.store
        self.turn = 0

    def enqueue(self, db, kind, provider, payload, due=None):
        raw = json.dumps(payload, sort_keys=True)
        key = hashlib.sha256((kind+provider+raw).encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO sync_jobs(id,kind,provider,payload,due) VALUES (?,?,?,?,?)',
                   (key, kind, provider, raw, time.time() if due is None else due))

    def seed(self):
        with self.store.connect() as db:
            queries = [r['query'] for r in db.execute('SELECT DISTINCT query FROM queries')]
            for provider in self.catalog.config['providers']:
                for query in ['', *queries]:
                    self.enqueue(db, 'search', provider, {'query': query})
                if self.catalog.metadata and self.catalog.config.get('archive_enabled', True):
                    paths = [f'/browse.php?c={c}&page=0' for c in (1001, 1002, 1003, 1006)] if provider == 'kinozal' else ['/forum/index.php']
                    for path in paths:
                        self.enqueue(db, 'archive', provider, {'path': path})
            for row in db.execute('SELECT data FROM releases').fetchall():
                item = json.loads(row['data'])
                if item['provider'] in self.catalog.config['providers'] and self.catalog.enabled(item):
                    self.enqueue(db, 'search', item['provider'], {'query': item['title']})
                    self.enqueue(db, 'detail', item['provider'], {'release': item['id']})

    def progress(self):
        with self.store.connect() as db:
            rows = db.execute('SELECT kind,count(*) total,sum(completed>0) visited,sum(attempts>0) retrying,min(CASE WHEN completed=0 THEN due END) next_due FROM sync_jobs GROUP BY kind').fetchall()
            known = db.execute('SELECT count(*) FROM releases').fetchone()[0]
            last = db.execute('SELECT max(completed) FROM sync_jobs').fetchone()[0]
        return {'known_releases': known, 'last_success': last, 'jobs': [dict(r) for r in rows], 'interval_seconds': self.catalog.config['sync_seconds']}

    async def step(self):
        kinds = ('archive', 'search', 'detail')
        job = None
        with self.store.connect() as db:
            for i in range(3):
                kind = kinds[(self.turn+i)%3]
                job = db.execute('SELECT * FROM sync_jobs WHERE kind=? AND due<=? ORDER BY due LIMIT 1', (kind, time.time())).fetchone()
                if job:
                    self.turn = (self.turn+i+1)%3; break
        if not job:
            return False
        provider, payload = job['provider'], json.loads(job['payload'])
        if provider not in self.catalog.config['providers']:
            with self.store.connect() as db:
                db.execute('DELETE FROM sync_jobs WHERE id=?', (job['id'],))
            return True
        try:
            if job['kind'] == 'search':
                error = await self.catalog.provider_search(provider, payload['query'])
                if error:
                    raise ValueError('Provider unavailable')
            elif job['kind'] == 'detail':
                item = await self.catalog.detail(payload['release'])
                if item and item.get('metadata_at', 0) < time.time()-86400+901 and self.catalog.jackett:
                    raise ValueError('Detail temporarily unavailable')
            else:
                rows, links = await archive.page(self.catalog.metadata, provider, payload['path'])
                from .catalog import normalize
                fingerprint = hashlib.sha256(json.dumps([r['Id'] for r in rows]).encode()).hexdigest()
                with self.store.connect() as db:
                    prior_page = db.execute('SELECT value FROM sync_state WHERE key=?', ('fingerprint:'+provider+fingerprint,)).fetchone()
                    if rows and prior_page and prior_page['value'] != payload['path']:
                        links = [path for path in links if 'start=' not in path and 'page=' not in path]
                    if rows:
                        db.execute('INSERT OR IGNORE INTO sync_state VALUES (?,?)', ('fingerprint:'+provider+fingerprint, payload['path']))
                    for row in rows:
                        item = normalize(provider, row)
                        if item:
                            prior = self.catalog.release(item['id'])
                            if prior:
                                item = prior | {k: item[k] for k in ('seeds', 'peers', 'size', 'checked_at')}
                            self.catalog.put(item, db)
                            self.enqueue(db, 'detail', provider, {'release': item['id']})
                    db.execute('INSERT OR REPLACE INTO sync_state VALUES (?,?)', ('page:'+provider+payload['path'], fingerprint))
                    for path in links:
                        self.enqueue(db, 'archive', provider, {'path': path}, time.time()+self.catalog.config.get('sync_request_delay', 3))
            with self.store.connect() as db:
                db.execute("UPDATE sync_jobs SET completed=?,due=?,attempts=0,error='' WHERE id=?", (time.time(), time.time()+self.catalog.config['sync_seconds'], job['id']))
        except (ValueError, OSError, TimeoutError, KeyError, RuntimeError, httpx.HTTPError) as error:
            with self.store.connect() as db:
                delay = min(self.catalog.config['sync_seconds'], 30*2**min(job['attempts'], 10))
                db.execute('UPDATE sync_jobs SET attempts=attempts+1,error=?,due=? WHERE id=?', (type(error).__name__, time.time()+delay, job['id']))
        self.catalog.sync_status = self.progress()
        return True

    async def run(self):
        seeded = 0
        while True:
            if time.monotonic()-seeded > 300:
                self.seed(); seeded = time.monotonic()
            self.catalog.sync_status = self.progress()
            await self.step()
            await asyncio.sleep(self.catalog.config.get('sync_request_delay', 3))
