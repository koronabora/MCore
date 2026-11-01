import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
import httpx
from rapidfuzz.fuzz import WRatio
from .jackett import Jackett

log = logging.getLogger(__name__)


def number(value):
    try:
        return int(re.sub(r'[^0-9]', '', str(value)))
    except ValueError:
        return 0


def normalize(provider, row):
    title = str(row.get('Title') or row.get('Name') or row.get('title') or '').strip()
    rid = str(row.get('Id') or row.get('id') or '')
    if not rid.isdigit():
        link = str(row.get('link', ''))
        match = re.search(r'(?:[?&](?:t|id)=|/torrent/)(\d+)', link)
        rid = match[1] if match else ''
    if not title or not rid:
        return None
    year = re.search(r'\b(?:19|20)\d{2}\b', str(row.get('Year') or title))
    clean = re.split(r'\s*[\[(]|\s*/\s*|\b(?:19|20)\d{2}\b', title)[0].strip(' -./') or title
    canonical = re.sub(r'\W+', ' ', unicodedata.normalize('NFKC', clean).casefold()).strip()
    # Unknown year is deliberately not merged across sources.
    identity = canonical + ':' + (year[0] if year else provider + ':' + rid)
    resolution = re.search(r'\b(2160|1080|720|480)[pi]?\b', title)
    return {'id': provider + ':' + rid, 'movie': hashlib.sha256(identity.encode()).hexdigest()[:24],
            'provider': provider, 'source_id': rid, 'title': clean, 'release_title': title,
            'year': year[0] if year else '', 'director': '', 'description': '', 'poster': '',
            'resolution': resolution[1] + 'p' if resolution else 'Original',
            'size': str(row.get('Size') or row.get('size') or ''),
            'seeds': number(row.get('Seeds', row.get('seeds', 0))),
            'peers': number(row.get('Peers', row.get('peers', 0))),
            'magnet': row.get('Magnet') or row.get('magnet') or '',
            'audio': '', 'screenshots': [], 'checked_at': time.time()}


class Catalog:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.client = httpx.AsyncClient(base_url=config['torapi_url'], timeout=35, trust_env=False)
        self.limit = asyncio.Semaphore(4)
        self.sync_status = {}
        self.jackett = Jackett(config['jackett']) if config.get('jackett') else None

    async def close(self):
        await self.client.aclose()
        if self.jackett:
            await self.jackett.client.aclose()

    def put(self, item):
        with self.store.connect() as db:
            prior = db.execute('SELECT data FROM releases WHERE id=?', (item['id'],)).fetchone()
            if prior:
                old = json.loads(prior['data'])
                item['search_queries'] = list(dict.fromkeys(item.get('search_queries', []) + old.get('search_queries', [])))[:16]
                for field in ('director', 'description', 'poster', 'magnet', 'audio', 'screenshots'):
                    item[field] = item.get(field) or old.get(field, '')
            db.execute('INSERT OR REPLACE INTO releases VALUES (?,?,?,?)',
                       (item['id'], item['movie'], json.dumps(item, ensure_ascii=False), time.time()))

    async def provider_search(self, provider, query):
        if provider not in self.config['providers']:
            return None
        try:
            async with self.limit:
                if self.jackett:
                    for row in await self.jackett.search(provider, query):
                        item = normalize(provider, row)
                        if item:
                            item.update(download=row['download'], description=row['description'], poster=row['poster'],
                                        search_queries=[query.casefold()] if query else [])
                            self.put(item)
                    return None
                response = await self.client.get('/api/search/title/' + provider, params={'query': query})
                if response.status_code == 400:
                    return None
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows, list):
                    raise ValueError('Unexpected provider response')
                for row in rows:
                    item = normalize(provider, row)
                    if item:
                        self.put(item)
            return None
        except (httpx.HTTPError, ValueError, TypeError):
            log.warning('Provider search unavailable: %s', provider)
            return provider

    def movies(self, query=''):
        with self.store.connect() as db:
            rows = db.execute('SELECT data FROM releases ORDER BY updated DESC LIMIT 20000').fetchall()
        grouped = {}
        for row in rows:
            item = json.loads(row['data'])
            if not self.enabled(item):
                continue
            score = WRatio(query.casefold(), item['title'].casefold()) if query else 100
            if query and query.casefold() in item.get('search_queries', []):
                score = 100
            if query and score < 65:
                continue
            mid = item['movie']
            if mid not in grouped:
                grouped[mid] = {k: item[k] for k in ('title', 'year', 'director', 'description')}
                grouped[mid].update(id=mid, releases=[], score=score)
            grouped[mid]['releases'].append(self.public(item))
        return sorted(grouped.values(), key=lambda m: -m['score'])[:100]

    @staticmethod
    def public(item):
        return {k: v for k, v in item.items() if k not in ('magnet', 'poster', 'screenshots', 'download', 'torrent_data', 'search_queries')}

    def release(self, rid):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM releases WHERE id=?', (rid,)).fetchone()
        item = json.loads(row['data']) if row else None
        return item if item and self.enabled(item) else None

    def enabled(self, item):
        return item['provider'] == 'local' or item['provider'] in self.config['providers']

    def movie_releases(self, mid):
        with self.store.connect() as db:
            rows = db.execute('SELECT data FROM releases WHERE movie=?', (mid,)).fetchall()
        return [item for row in rows if self.enabled(item := json.loads(row['data']))]

    async def detail(self, rid):
        item = self.release(rid)
        if item is None:
            return None
        if item['provider'] == 'local':
            return item
        if self.jackett:
            return item
        try:
            async with self.limit:
                response = await self.client.get('/api/search/id/' + item['provider'], params={'query': item['source_id']})
                response.raise_for_status()
                data = response.json()
            row = data[0] if isinstance(data, list) else data
            for dest, source in [('description', 'Description'), ('director', 'Directer'),
                                 ('poster', 'Poster'), ('audio', 'Audio'), ('magnet', 'Magnet')]:
                item[dest] = row.get(source) or item.get(dest, '')
            screenshots = row.get('Screenshots')
            if isinstance(screenshots, list):
                item['screenshots'] = [s for s in screenshots if isinstance(s, str)][:3]
            if not item['magnet'] and re.fullmatch('[a-fA-F0-9]{40}', str(row.get('Hash', ''))):
                item['magnet'] = 'magnet:?xt=urn:btih:' + row['Hash']
            # Detail responses do not always contain swarm counters; do not invent them.
            if 'Seeds' in row:
                item['seeds'] = number(row['Seeds'])
            if 'Peers' in row:
                item['peers'] = number(row['Peers'])
            self.put(item)
        except (httpx.HTTPError, ValueError, TypeError, IndexError):
            log.warning('Provider detail unavailable: %s', rid)
        return item

    async def movie(self, mid):
        items = self.movie_releases(mid)
        if not items:
            return None
        # Search refresh updates seed/peer counts which many detail endpoints omit.
        await asyncio.gather(*(self.provider_search(p, items[0]['title']) for p in {i['provider'] for i in items}))
        await asyncio.gather(*(self.detail(item['id']) for item in items[:12]))
        items = self.movie_releases(mid)
        best = max(items, key=lambda i: len(i.get('description', '')))
        return {'id': mid, **{k: best[k] for k in ('title', 'year', 'director', 'description')},
                'releases': [self.public(i) for i in items]}

    async def synchronize(self):
        while True:
            started = time.monotonic()
            try:
                with self.store.connect() as db:
                    queries = [r['query'] for r in db.execute('SELECT DISTINCT query FROM queries')]
                    releases = [item['id'] for r in db.execute('SELECT data FROM releases ORDER BY updated')
                                if (item := json.loads(r['data']))['provider'] in self.config['providers']]
                    known = {(item['provider'], item['title']) for item in
                             (json.loads(r['data']) for r in db.execute('SELECT data FROM releases'))
                             if item['provider'] in self.config['providers']}
                for provider in self.config['providers']:
                    if self.jackett:
                        await self.provider_search(provider, '')
                        continue
                    try:
                        response = await self.client.get('/api/get/rss/' + provider, headers={'Accept': 'application/json'})
                        response.raise_for_status()
                        for row in response.json():
                            item = normalize(provider, row)
                            if item:
                                self.put(item)
                    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                        log.warning('RSS unavailable: %s', provider)
                for query in queries:
                    await asyncio.gather(*(self.provider_search(p, query) for p in self.config['providers']))
                pairs = list(known)
                for offset in range(0, len(pairs), 4):
                    await asyncio.gather(*(self.provider_search(p, q) for p, q in pairs[offset:offset+4]))
                for offset in range(0, len(releases), 4):
                    await asyncio.gather(*(self.detail(rid) for rid in releases[offset:offset+4]))
                self.sync_status = {'completed_at': time.time(), 'known_refreshed': len(releases)}
            except Exception:
                log.exception('Background index refresh failed')
            await asyncio.sleep(max(60, self.config['sync_seconds']-(time.monotonic()-started)))
