import asyncio
from contextlib import nullcontext
import hashlib
import json
import logging
import re
import time
import unicodedata
import httpx
from rapidfuzz.fuzz import WRatio
from .jackett import Jackett
from .metadata import Metadata

log = logging.getLogger(__name__)


def number(value):
    try:
        return int(re.sub(r'[^0-9]', '', str(value)))
    except ValueError:
        return 0


def title_key(value):
    return re.sub(r'\W+', ' ', unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')).strip()


SEASON = re.compile(r'(?i)\b(?:s(\d{1,3}(?:-\d{1,3})?)(?:e\d+)?|(\d{1,3}(?:-\d{1,3})?)\s*сезон\w*|(?:сезоны?|seasons?)\s*[: .-]*([\d,-]+)|(\d{1,2})x\d+|сери[ияй]\s*[: .-]*\d+)')


def video_title(title):
    return not re.search(r'(?i)\b(?:ArtBook|Wallpapers?|OST|Soundtrack)\b|\[(?:JPG|PNG|BMP|PDF|EPUB|MP3|FLAC)(?:[/,\s\w]*)\]|Anime Creditless|Fan-Made Trailer|[,\[]\s*трейлер\b|русский фан-ролик', title)


def normalize(provider, row):
    title = str(row.get('Title') or row.get('Name') or row.get('title') or '').strip()
    rid = str(row.get('Id') or row.get('id') or '')
    if not rid.isdigit():
        link = str(row.get('link', ''))
        match = re.search(r'(?:[?&](?:t|id)=|/torrent/)(\d+)', link)
        rid = match[1] if match else ''
    if not title or not rid or not video_title(title):
        return None
    year = re.search(r'\b(?:19|20)\d{2}\b', str(row.get('Year') or title))
    season = SEASON.search(title)
    title_part = re.sub(r'\([^)]*(?:сезон|сери[яий]|\bS\d|\bseason)[^)]*\)', '', title, flags=re.I)
    outside_season = SEASON.search(title_part)
    title_part = title_part[:outside_season.start()] if outside_season else title_part
    title_part = re.sub(r'^(?:National Geographic|Discovery(?: Channel)?|BBC)\s*[.:\-]?\s+', '', title_part, flags=re.I)
    title_part = re.sub(r"(?i)\((?:режисс[её]рская версия|director'?s cut|IMAX(?: Edition)?|extended(?: cut| edition)?)\)", '', title_part)
    clean = re.split(r'\s*[\[(]|\s*/\s*|\b(?:19|20)\d{2}\b', title_part)[0].strip(' -./') or title
    aliases = [clean]
    bilingual = re.split(r'(?<=[а-яё])\s+(?=[A-Za-z])', clean, maxsplit=1, flags=re.I)
    if len(bilingual) == 2 and re.search('[а-яё]', bilingual[0], re.I):
        clean = bilingual[0].strip(' -./')
        aliases = bilingual
    # Preserve alternate-language titles for exact search, without treating every query as exact.
    before_year = re.split(r'[\[(]|\b(?:19|20)\d{2}\b', title_part)[0]
    for part in before_year.split('/')[:8]:
        alias = re.split(r'[\[(]', part)[0].strip(' -./')
        if alias and alias not in aliases:
            aliases.append(alias)
    translated = re.search(r'\)\s+/?\s*([A-Za-z][A-Za-z0-9 .:\'’-]+?)\s*[-/]*\s*$', title_part)
    if translated:
        aliases.append(translated[1].strip(' -/'))
    original = next((a for a in aliases if re.search('[a-z]', a, re.I) and not re.search('[а-яё]', a, re.I)), clean)
    canonical = title_key(original)
    series = bool(season) or bool(row.get('series')) or bool(re.search(r'(?i)\[TV\]|\bсериал\b|\b\d+\s+сери[яий]', title))
    anime = bool(re.search(r'(?i)\[TV\]|\bаниме\b|сёнэн', title))
    # Unknown year is deliberately not merged across sources.
    identity = canonical + ':' + ('anime-series' if series and anime else 'series' if series else year[0] if year else provider + ':' + rid)
    resolution = re.search(r'\b(2160|1080|720|576|480)[piр]?\b', title, re.I)
    dimensions = re.search(r'\b\d{3,4}\s*[xх×]\s*(\d{3,4})\b', title, re.I)
    source = re.search(r'(?i)\b(?:UHD\s*BDRemux|BDRemux|BDRip|Blu-?Ray(?:\s*Remux)?|WEB-?DL|WEBRip|HDRip|HDTV|DVDRip|DVD[-59]?|CAMRip|TS)\b', title)
    return {'id': provider + ':' + rid, 'movie': hashlib.sha256(identity.encode()).hexdigest()[:24], 'parser_version': 7,
            'title_aliases': aliases,
            'provider': provider, 'source_id': rid, 'title': clean, 'release_title': title,
            'year': year[0] if year else '', 'director': '', 'description': '', 'poster': '',
            'resolution': resolution[1] + ('i' if resolution[0].lower().endswith('i') else 'p') if resolution else dimensions[0] if dimensions else '2160p' if re.search(r'(?i)\b(?:4K|UHD)\b', title) else source[0] + ' · разрешение не указано' if source else 'Разрешение не указано',
            'series': series, 'season': next((g for g in season.groups() if g), '') if season else '',
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
        self.metadata = Metadata(config['jackett']) if self.jackett else None
        self.detail_locks = {}
        self.refresh_tasks = set()
        with store.connect() as db:
            existing = [json.loads(row['data']) for row in db.execute('SELECT data FROM releases')]
            # Reindex as one transaction; thousands of individual fsyncs delay API startup.
            for old in existing:
                director = re.split(r'\s+(?:О фильме|О сериале|Описание|В ролях|Акт[её]ры|Жанр|Страна|Год выпуска)\s*:', old.get('director',''), maxsplit=1, flags=re.I)[0]
                if director != old.get('director',''):
                    old['director'] = director
                    db.execute('UPDATE releases SET data=? WHERE id=?', (json.dumps(old,ensure_ascii=False),old['id']))
                if not self.enabled(old) or old['provider'] == 'local' or old.get('parser_version') == 7:
                    continue
                parsed = normalize(old['provider'], {'Id': old['source_id'], 'Title': old['release_title'], 'series': old.get('detail_series')})
                if parsed:
                    updated = old | {k: parsed[k] for k in ('movie', 'title', 'resolution', 'series', 'season', 'title_aliases')}
                    updated['parser_version'] = 7
                    updated['metadata_at'] = 0
                    self.put(updated, db)
            if not db.execute("SELECT 1 FROM sync_state WHERE key='identity-reconcile-031'").fetchone():
                for row in db.execute('SELECT id FROM releases').fetchall():
                    item = json.loads(db.execute('SELECT data FROM releases WHERE id=?', (row['id'],)).fetchone()['data'])
                    if self.enabled(item):
                        self.put(item, db)
                db.execute("INSERT INTO sync_state VALUES ('identity-reconcile-031','done')")

    async def close(self):
        for task in self.refresh_tasks:
            task.cancel()
        await asyncio.gather(*self.refresh_tasks, return_exceptions=True)
        await self.client.aclose()
        if self.jackett:
            await self.jackett.client.aclose()
            await self.metadata.client.aclose()

    def put(self, item, connection=None):
        with (nullcontext(connection) if connection is not None else self.store.connect()) as db:
            prior = db.execute('SELECT data FROM releases WHERE id=?', (item['id'],)).fetchone()
            if prior:
                old = json.loads(prior['data'])
                if old.get('parser_version') == 7 and old.get('detail_verified') and 'metadata_at' not in item:
                    for field in ('title', 'release_title', 'title_aliases', 'movie', 'series', 'season'):
                        item[field] = old[field]
                    item['detail_verified'] = True
                if item.get('parser_version') == old.get('parser_version') and item['movie'] in old.get('movie_aliases', []):
                    item['movie'] = old['movie']
                item['movie_aliases'] = list(dict.fromkeys(old.get('movie_aliases', []) + [old['movie']]))
                item['search_queries'] = list(dict.fromkeys(item.get('search_queries', []) + old.get('search_queries', [])))[:16]
                for field in ('director', 'description', 'poster', 'magnet', 'audio', 'screenshots'):
                    item[field] = item.get(field) or old.get(field, '')
                for field in ('metadata_at', 'dimensions', 'torrent_id', 'external_ids', 'screenshots_at', 'detail_series'):
                    if field not in item and field in old:
                        item[field] = old[field]
                if item.get('dimensions') and 'не указано' in item['resolution']:
                    item['resolution'] = item['dimensions']
            before = item['movie']
            kind = 'anime' if re.search(r'(?i)\[TV\]|\bаниме\b|сёнэн', item['release_title']) else 'series' if item.get('series') else 'film'
            year = item.get('year', '') if kind == 'film' else ''
            aliases = {title_key(a) for a in item.get('title_aliases', [])+[item['title']] if a}
            ids = item.get('external_ids', {})
            strong = {f'{source}:{value}' for source, value in ids.items() if source in ('imdb', 'kinopoisk') and value}
            candidates = set()
            for alias in strong:
                candidates.update(r['movie'] for r in db.execute('SELECT movie FROM title_links WHERE alias=? AND kind=? AND year=?', (alias, kind, '')))
            for alias in aliases if year or kind == 'series' else set():
                candidates.update(r['movie'] for r in db.execute('SELECT movie FROM title_links WHERE alias=? AND kind=? AND year=?', (alias, kind, year)))
            groups = {mid: [json.loads(r['data']) for r in db.execute('SELECT data FROM releases WHERE movie=?', (mid,))] for mid in candidates}
            groups = {mid: rows for mid, rows in groups.items() if rows}
            identifiers = {k: {v} for k, v in ids.items()}
            for rows in groups.values():
                for other in rows:
                    for k, v in other.get('external_ids', {}).items():
                        identifiers.setdefault(k, set()).add(v)
            conflict = any(len(values) > 1 for values in identifiers.values())
            if groups and not conflict:
                target = before if before in groups else sorted(groups)[0]
                item['movie'] = target
                for previous, rows in groups.items():
                    if previous == target:
                        continue
                    for other in rows:
                        other['movie'] = target
                        other['movie_aliases'] = list(dict.fromkeys(other.get('movie_aliases', [])+[previous]))
                        db.execute('UPDATE releases SET movie=?,data=? WHERE id=?', (target,json.dumps(other,ensure_ascii=False),other['id']))
                    db.execute('INSERT OR IGNORE INTO title_links SELECT alias,kind,year,? FROM title_links WHERE movie=?', (target,previous))
                    db.execute('DELETE FROM title_links WHERE movie=?', (previous,))
                    item['movie_aliases'] = list(dict.fromkeys(item.get('movie_aliases', [])+[previous]))
            elif not candidates and strong:
                item['movie'] = hashlib.sha256((kind+':'+sorted(strong)[0]).encode()).hexdigest()[:24]
            if before != item['movie']:
                item['movie_aliases'] = list(dict.fromkeys(item.get('movie_aliases', [])+[before]))
            db.execute('INSERT OR REPLACE INTO releases VALUES (?,?,?,?)',
                       (item['id'], item['movie'], json.dumps(item, ensure_ascii=False), time.time()))
            for alias in aliases:
                db.execute('INSERT OR IGNORE INTO title_links VALUES (?,?,?,?)', (alias, kind, year, item['movie']))
            for alias in strong:
                db.execute('INSERT OR IGNORE INTO title_links VALUES (?,?,?,?)', (alias, kind, '', item['movie']))
            if prior and old['movie'] != item['movie'] and not db.execute('SELECT 1 FROM releases WHERE movie=? LIMIT 1', (old['movie'],)).fetchone():
                db.execute('DELETE FROM title_links WHERE movie=?', (old['movie'],))

    async def provider_search(self, provider, query):
        if provider not in self.config['providers']:
            return None
        try:
            async with self.limit:
                if self.jackett:
                    rows = await self.jackett.search(provider, query)
                    with self.store.connect() as db:
                        for row in rows:
                            item = normalize(provider, row)
                            if item:
                                item.update(download=row['download'], description=row['description'], poster=row['poster'],
                                            search_queries=[query.casefold()] if query else [])
                                self.put(item, db)
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
            rows = db.execute('SELECT data FROM releases ORDER BY updated DESC').fetchall()
        grouped = {}
        for row in rows:
            item = json.loads(row['data'])
            if not self.enabled(item):
                continue
            score = WRatio(query.casefold(), item['title'].casefold()) if query else 100
            exact = bool(query) and title_key(query) in {title_key(t) for t in item.get('title_aliases', []) + [item['title']]}
            if exact:
                score = 120 if title_key(query) == title_key(item['title']) else 100
            if query and query.casefold() in item.get('search_queries', []):
                score = max(score, 65)
            if query and score < 65:
                continue
            mid = item['movie']
            if mid not in grouped:
                grouped[mid] = {k: item[k] for k in ('title', 'year', 'director', 'description')}
                grouped[mid].update(id=mid, releases=[], score=score, exact=exact, series=item.get('series', False))
            grouped[mid]['score'] = max(grouped[mid]['score'], score)
            grouped[mid]['exact'] |= exact
            for field in ('description', 'director'):
                if not grouped[mid][field] and item.get(field):
                    grouped[mid][field] = item[field]
            grouped[mid]['releases'].append(self.public(item))
        return sorted(grouped.values(), key=lambda m: (not m['exact'], -m['score'], m['series'], m['title']))[:100]

    @staticmethod
    def public(item):
        return {k: v for k, v in item.items() if k not in ('magnet', 'poster', 'screenshots', 'download', 'torrent_data', 'search_queries')}

    def release(self, rid):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM releases WHERE id=?', (rid,)).fetchone()
        item = json.loads(row['data']) if row else None
        return item if item and self.enabled(item) else None

    def enabled(self, item):
        return item['provider'] == 'local' or (item['provider'] in self.config['providers'] and video_title(item.get('release_title', '')))

    def movie_releases(self, mid):
        with self.store.connect() as db:
            items = [json.loads(row['data']) for row in db.execute('SELECT data FROM releases WHERE movie=?', (mid,))]
            current = [i for i in items if self.enabled(i)]
            if current:
                return current
            aliases = [json.loads(row['data']) for row in db.execute("SELECT data FROM releases WHERE EXISTS (SELECT 1 FROM json_each(releases.data, '$.movie_aliases') WHERE value=?)", (mid,))]
            identities = {i['movie'] for i in aliases if self.enabled(i)}
            if not identities:
                return []
            rows = db.execute('SELECT data FROM releases WHERE movie IN (' + ','.join('?' for _ in identities) + ')', tuple(identities))
            return [i for row in rows if self.enabled(i := json.loads(row['data']))]

    async def detail(self, rid):
        item = self.release(rid)
        if item is None:
            return None
        if item['provider'] == 'local':
            return item
        if self.jackett:
            if item.get('metadata_at', 0) < time.time()-86400:
                lock = self.detail_locks.setdefault(rid, asyncio.Lock())
                async with lock:
                    item = self.release(rid)
                    if item.get('metadata_at', 0) < time.time()-86400:
                        try:
                            item.update(await self.metadata.detail(item['provider'], item['source_id']))
                            if item.get('detail_title'):
                                parsed = normalize(item['provider'], {'Id': item['source_id'], 'Title': item.pop('detail_title'), 'series': item.get('detail_series')})
                                if parsed:
                                    for field in ('title', 'title_aliases', 'movie', 'series', 'season', 'release_title'):
                                        item[field] = parsed[field]
                            item['metadata_at'] = time.time()
                            item['detail_verified'] = True
                        except (httpx.HTTPError, ValueError, KeyError):
                            item['metadata_at'] = time.time()-86400+900
                        self.put(item)
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
        # Return the refreshed counters in this response, not in a discarded background task.
        refresh_started = time.time()
        async def refresh(provider):
            try:
                return await asyncio.wait_for(self.provider_search(provider, items[0]['title']), timeout=20)
            except TimeoutError:
                return provider
        providers = {i['provider'] for i in items if i['provider'] in self.config['providers']}
        failed = [p for p in await asyncio.gather(*(refresh(p) for p in providers)) if p]
        # One useful detail per card is enough for its poster and synopsis.
        if not any(i.get('description') and i.get('poster') for i in items):
            for item in sorted(items, key=lambda i: (i['provider'] != 'kinozal', -i.get('seeds', 0)))[:2]:
                try:
                    detailed = await asyncio.wait_for(self.detail(item['id']), timeout=30)
                except TimeoutError:
                    continue
                if detailed and detailed.get('description') and detailed.get('poster'):
                    break
        items = self.movie_releases(mid)
        best = max(items, key=lambda i: len(i.get('description', '')))
        return {'id': mid, **{k: best[k] for k in ('title', 'year', 'director', 'description')},
                'releases': [self.public(i) | {'swarm_stale': i['provider'] != 'local' and i.get('checked_at', 0) < refresh_started}
                             for i in sorted(items, key=lambda i: (i.get('season', ''), -i.get('seeds', 0)))],
                'swarm_refresh_failed': failed,
                'aliases': list({mid, *(a for i in items for a in i.get('movie_aliases', []) if a), *(i['movie'] for i in items)})}

    async def enrich(self, movies):
        async def one(movie):
            releases = self.movie_releases(movie['id'])
            if any(r.get('description') and r.get('director') and r.get('poster') for r in releases):
                return
            best = sorted(releases, key=lambda r: (r['provider'] != 'kinozal', -r.get('seeds', 0)))
            if best:
                try:
                    await asyncio.wait_for(self.detail(best[0]['id']), 20)
                except TimeoutError:
                    pass
        await asyncio.gather(*(one(movie) for movie in movies[:12]))

    async def synchronize(self):
        from .sync import Synchronizer
        await Synchronizer(self).run()
