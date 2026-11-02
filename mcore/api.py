import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import json
import hashlib
import time
import re
from pathlib import PurePath
from urllib.parse import quote
import httpx
from fastapi import FastAPI, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from .config import load
from .store import Store
from .catalog import Catalog
from .torrents import Torrents, byte_range
from .images import image_bytes
from .inspection import Inspection
from . import episodes
from . import telemetry
from .source_index import SourceIndex


@asynccontextmanager
async def lifespan(app):
    config = load()
    app.state.store = Store(config['state_dir'])
    app.state.catalog = Catalog(app.state.store, config)
    app.state.torrents = Torrents(app.state.store, config)
    app.state.inspection = Inspection(app.state.torrents, config['state_dir'])
    async def index_start(rid):
        return (await start(Start(release=rid), uid='indexer'))['id']
    app.state.source_index = SourceIndex(app.state.store, app.state.catalog, app.state.torrents, index_start)
    tasks = [asyncio.create_task(app.state.catalog.synchronize()), asyncio.create_task(app.state.torrents.maintain()), asyncio.create_task(app.state.torrents.alerts()), asyncio.create_task(app.state.source_index.run()), asyncio.create_task(telemetry.publish(app.state.torrents, config['state_dir']))]
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await app.state.inspection.close()
    await app.state.catalog.close()
    app.state.torrents.session.pause()


app = FastAPI(title='MCore', version='0.3.9', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
attempts = defaultdict(deque)


@app.middleware('http')
async def limits(request: Request, call_next):
    if int(request.headers.get('content-length', '0')) > 16384:
        return Response(status_code=413)
    if request.url.path in ('/v1/pair', '/v1/search'):
        now = time.monotonic()
        for key in list(attempts):
            if not attempts[key] or attempts[key][-1] < now-60:
                del attempts[key]
        key = (request.client.host, request.url.path)
        queue = attempts[key]
        while queue and queue[0] < now-60:
            queue.popleft()
        if len(queue) >= 30:
            return Response(status_code=429, headers={'Retry-After': '60'})
        queue.append(now)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def user(authorization: str = Header(default='')):
    if not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Authentication required')
    uid = app.state.store.authenticate(authorization[7:])
    if not uid:
        raise HTTPException(401, 'Invalid or revoked key')
    return uid


class Pair(BaseModel):
    token: str = Field(min_length=32, max_length=100)
    device: str = Field(default='', max_length=128)


@app.post('/v1/pair')
def pair(body: Pair):
    result = app.state.store.redeem(body.token, body.device)
    if not result:
        raise HTTPException(401, 'Invitation expired or already used')
    return result


@app.get('/v1/health')
def health(uid=Depends(user)):
    return {'status': 'ok', 'service': 'MCore', 'version': app.version, 'sync': app.state.catalog.sync_status}


@app.get('/v1/search')
async def search(q: str = Query(min_length=1, max_length=150), live: bool = True, uid=Depends(user)):
    q = q.strip()
    if not q:
        raise HTTPException(400, 'Empty query')
    catalog = app.state.catalog
    if live:
        with app.state.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO queries VALUES (?,?,?)', (uid, q, time.time()))
    async def refresh(provider):
        from .catalog import title_key
        normalized = title_key(q)
        variants = list(dict.fromkeys([normalized, re.sub(r'\s+', '-', normalized)])) if len(normalized.split()) == 2 else [normalized]
        try:
            outcomes = await asyncio.wait_for(asyncio.gather(*(catalog.provider_search(provider, value) for value in variants)), 25)
            return provider if all(outcomes) else None
        except TimeoutError:
            return provider
    errors = await asyncio.gather(*(refresh(p) for p in catalog.config['providers'])) if live else []
    # Suggestions must not wait for tracker detail pages. Posters hydrate independently.
    return {'movies': catalog.movies(q), 'unavailable_providers': [e for e in errors if e]}


@app.get('/v1/movies/{mid}')
async def movie(mid: str, uid=Depends(user)):
    # Index known sources while refreshing counters and the card's description.
    app.state.source_index.schedule(app.state.catalog.movie_releases(mid))
    result = await app.state.catalog.movie(mid)
    if result is None:
        raise HTTPException(404, 'Movie not found')
    aliases = result.get('aliases', [mid])
    with app.state.store.connect() as db:
        row = db.execute('SELECT position,torrent,file FROM history WHERE user=? AND movie IN (' + ','.join('?' for _ in aliases) + ') ORDER BY updated DESC LIMIT 1', (uid, *aliases)).fetchone()
    result['position'] = row['position'] if row else 0
    result['resume'] = {'torrent': row['torrent'], 'file': row['file']} if row and row['torrent'] else None
    if result['resume']:
        result['resume']['release'] = next((i['id'] for i in app.state.catalog.movie_releases(mid) if i.get('torrent_id') == row['torrent']), '')
    result['episodes'] = episodes.listing(app.state.store, aliases, uid)
    releases = app.state.catalog.movie_releases(mid)
    app.state.source_index.schedule(releases)
    for release in releases:
        key = release.get('torrent_id')
        if key in app.state.torrents.handles:
            files = app.state.torrents.files(key)
            if files:
                episodes.register(app.state.store, release, key, files)
    result.update(episodes.catalog(app.state.store, releases, aliases, uid))
    result['releases'] = [app.state.catalog.public(r) for r in releases]
    return result


@app.get('/v1/movies/{mid}/sources')
def movie_sources(mid: str, uid=Depends(user)):
    releases = app.state.catalog.movie_releases(mid)
    if not releases:
        raise HTTPException(404, 'Movie not found')
    aliases = list({mid, *(r['movie'] for r in releases), *(a for r in releases for a in r.get('movie_aliases', []))})
    result = episodes.catalog(app.state.store, releases, aliases, uid)
    result['releases'] = [app.state.catalog.public(r) for r in releases]
    return result


@app.get('/v1/library/match')
def match_saved(name: str = Query(min_length=1, max_length=500), uid=Depends(user)):
    """Conservative catalog-only association for legacy files without a sidecar."""
    stem = PurePath(name).stem.replace('.', ' ')
    year = re.search(r'\b(?:19|20)\d{2}\b', stem)
    title = re.split(r'(?i)[\[(]|\b(?:19|20)\d{2}\b|\bs\d{1,3}e\d+\b|\b\d{1,2}x\d+\b|\b(?:BDRip|WEB-DL|WEBRip|HDRip|1080p|720p|2160p)\b', stem)[0].strip(' -_')
    if not title:
        return {'movie': None}
    choices = [m for m in app.state.catalog.movies(title) if m['exact'] and (not year or m['year'] == year[0])]
    return {'movie': choices[0] if len(choices) == 1 else None}


@app.get('/v1/home')
def home(uid=Depends(user)):
    with app.state.store.connect() as db:
        queries = [r['query'] for r in db.execute('SELECT query FROM queries WHERE user=? ORDER BY updated DESC LIMIT 8', (uid,))]
        history = []
        seen = set()
        for row in db.execute('SELECT * FROM history WHERE user=? ORDER BY updated DESC LIMIT 30', (uid,)):
            releases = app.state.catalog.movie_releases(row['movie'])
            if releases:
                item = releases[0]
                identity = tuple(sorted(i['id'] for i in releases))
                if identity in seen:
                    continue
                seen.add(identity)
                history.append({'id': row['movie'], 'title': item['title'], 'year': item['year'], 'director': item.get('director',''),
                                'series': item.get('series',False), 'animated': any(app.state.catalog.animated(r) for r in releases), 'position': row['position']})
    return {'queries': queries, 'history': history}


@app.get('/v1/movies/{mid}/poster')
async def poster(mid: str, uid=Depends(user)):
    items = app.state.catalog.movie_releases(mid)
    url = next((item['poster'] for item in items if item.get('poster')), None)
    if not url:
        for item in sorted(items, key=lambda i: (i['provider'] != 'kinozal', -i.get('seeds', 0)))[:2]:
            detail = await app.state.catalog.detail(item['id'])
            if detail and detail.get('poster'):
                url = detail['poster']
                break
    if not url:
        raise HTTPException(404, 'Poster unavailable')
    urls = list(dict.fromkeys([url]+[i['poster'] for i in app.state.catalog.movie_releases(mid) if i.get('poster')]))
    content = None
    for candidate in urls[:3]:
        try:
            content, media_type = await image_bytes(candidate, poster=True)
            break
        except (ValueError, OSError, httpx.HTTPError):
            continue
    if content is None:
        raise HTTPException(502, 'Poster source unavailable')
    director = next((item.get('director') for item in app.state.catalog.movie_releases(mid) if item.get('director')), '')
    return Response(content, media_type=media_type, headers={'X-MCore-Director': quote(director)})


@app.get('/v1/releases/{rid}/screenshots/{index}')
async def screenshot(rid: str, index: int, uid=Depends(user)):
    if not 0 <= index <= 2:
        raise HTTPException(404, 'Screenshot not found')
    release = await app.state.catalog.detail(rid)
    if not release:
        raise HTTPException(404, 'Release not found')
    try:
        sources = release.get('screenshots') or []
        if not sources and release['provider'] == 'kinozal' and app.state.catalog.metadata:
            lock = app.state.catalog.detail_locks.setdefault('screens:'+rid, asyncio.Lock())
            async with lock:
                release = app.state.catalog.release(rid)
                if release.get('screenshots_at', 0) < time.time()-900:
                    release['screenshots'] = await app.state.catalog.metadata.screenshots('kinozal', release['source_id'])
                    release['screenshots_at'] = time.time()
                    app.state.catalog.put(release)
                sources = release.get('screenshots') or []
        if index < len(sources):
            content, media_type = await image_bytes(sources[index], screenshot=True)
        else:
            content, media_type = await app.state.inspection.frame(release, index), 'image/jpeg'
    except (ValueError, RuntimeError, KeyError, OSError, TimeoutError, httpx.HTTPError):
        raise HTTPException(404, 'Screenshot not yet available')
    return Response(content, media_type=media_type)


@app.get('/v1/torrents/{key}/files/{index}/info')
async def media_info(key: str, index: int, uid=Depends(user)):
    try:
        return await app.state.inspection.metadata(key, index)
    except (ValueError, KeyError, OSError, TimeoutError):
        raise HTTPException(409, 'Media metadata is not yet available')


class Position(BaseModel):
    position: int = Field(ge=0, le=604800000)
    torrent: str | None = Field(default=None, pattern=r'^[a-f0-9]{40,64}$')
    file: int | None = Field(default=None, ge=0)
    episode: str | None = Field(default=None, max_length=150)
    duration: int = Field(default=0, ge=0, le=604800000)
    completed: bool = False


@app.put('/v1/history/{mid}')
def history(mid: str, body: Position, uid=Depends(user)):
    with app.state.store.connect() as db:
        releases = app.state.catalog.movie_releases(mid)
        if not releases:
            raise HTTPException(404, 'Movie not found')
        mapping = db.execute('SELECT * FROM episode_files WHERE torrent=? AND file=?', (body.torrent, body.file)).fetchone()
        episode = body.episode or (mapping['episode'] if mapping else None)
        if episode:
            if not mapping or mapping['episode'] != episode or mapping['release'] not in {r['id'] for r in releases}:
                raise HTTPException(400, 'Episode does not belong to this movie/file')
            db.execute('''INSERT INTO episode_history VALUES (?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(user,movie,episode) DO UPDATE SET position=excluded.position,duration=excluded.duration,
                          completed=excluded.completed,torrent=excluded.torrent,file=excluded.file,updated=excluded.updated''',
                       (uid, releases[0]['movie'], episode, body.position, body.duration, int(body.completed), body.torrent, body.file, time.time()))
        db.execute('''INSERT INTO history(user,movie,position,updated,torrent,file) VALUES (?,?,?,?,?,?)
                      ON CONFLICT(user,movie) DO UPDATE SET position=excluded.position,updated=excluded.updated,
                      torrent=COALESCE(excluded.torrent,history.torrent),file=COALESCE(excluded.file,history.file)''',
                   (uid, mid, body.position, time.time(), body.torrent, body.file))
    return {'ok': True}


class Start(BaseModel):
    release: str = Field(max_length=100)


@app.post('/v1/torrents')
async def start(body: Start, uid=Depends(user)):
    lock = app.state.catalog.detail_locks.setdefault('torrent:'+body.release, asyncio.Lock())
    async with lock:
        cached = app.state.catalog.release(body.release)
        if cached and cached.get('torrent_id') in app.state.torrents.handles:
            if uid != 'indexer': app.state.torrents.activate(cached['torrent_id'])
            return {'id': cached['torrent_id']}
        return await start_release(body, metadata_only=uid == 'indexer')


async def start_release(body, metadata_only=False):
    release = app.state.catalog.release(body.release) if metadata_only else None
    # An existing torrent/link is sufficient for a file list; fetching the full
    # description first used to serialize indexing behind unrelated HTML requests.
    if metadata_only and release and release.get('torrent_id') in app.state.torrents.handles:
        return {'id': release['torrent_id']}
    if not release or not (release.get('download') or release.get('magnet')):
        release = await app.state.catalog.detail(body.release)
    if not release:
        raise HTTPException(404, 'Release not found')
    try:
        if app.state.catalog.jackett and not release.get('download') and not release.get('magnet'):
            from .catalog import title_key
            await app.state.catalog.provider_search(release['provider'], title_key(release['title']))
            release = app.state.catalog.release(body.release)
        if not release.get('download') and not release.get('magnet') and app.state.catalog.metadata:
            fresh = await app.state.catalog.metadata.detail(release['provider'], release['source_id'])
            release['magnet'] = fresh.get('magnet', '')
            app.state.catalog.put(release)
        source = release.get('magnet', '')
        if app.state.catalog.jackett and release.get('download'):
            source = await app.state.catalog.jackett.download(release['download'])
        if not source:
            raise HTTPException(409, detail={'code': 'source_missing', 'message': 'Источник не предоставил torrent или magnet. Выберите другую раздачу.'})
        key = await app.state.torrents.add(source, metadata_only=metadata_only)
        release['torrent_id'] = key
        app.state.catalog.put(release)
    except httpx.HTTPError:
        raise HTTPException(502, detail={'code':'tracker_unavailable', 'message':'Трекер временно не отдаёт раздачу. Повторите позже или выберите другую.'})
    except (ValueError, RuntimeError) as error:
        raise HTTPException(409, detail={'code':'torrent_invalid', 'message':'Не удалось прочитать данные торрента. Выберите другую раздачу.'}) from error
    return {'id': key}


@app.get('/v1/torrents/{key}')
async def status(key: str, file: int = Query(default=0, ge=0), buffer: int = Query(default=15, ge=1, le=120), position: int = Query(default=0, ge=0, le=604800000), uid=Depends(user)):
    try:
        result = app.state.torrents.status(key, file, buffer, position)
        if result.get('metadata_ready') and not result['files']:
            result['error'] = 'В раздаче нет поддерживаемых видеофайлов. Для образов дисков и папок Blu-ray выберите другую раздачу (например, MKV или MP4).'
        if result['files']:
            app.state.inspection.schedule(key, file)
            with app.state.store.connect() as db:
                rows = db.execute("SELECT data FROM releases WHERE json_extract(data,'$.torrent_id')=?", (key,)).fetchall()
            for row in rows:
                release = json.loads(row['data'])
                episodes.register(app.state.store, release, key, result['files'])
            with app.state.store.connect() as db:
                for item in result['files']:
                    mapping = db.execute('SELECT * FROM episode_files WHERE torrent=? AND file=?', (key, item['index'])).fetchone()
                    if mapping:
                        item.update(episode=mapping['episode'], season=mapping['season'], number=mapping['number'])
                        related = app.state.catalog.movie_releases(mapping['movie'])
                        aliases = list({mapping['movie'], *(r['movie'] for r in related), *(a for r in related for a in r.get('movie_aliases', []))})
                        saved = db.execute('SELECT * FROM episode_history WHERE user=? AND episode=? AND movie IN ('+','.join('?' for _ in aliases)+') ORDER BY updated DESC LIMIT 1', (uid, mapping['episode'], *aliases)).fetchone()
                        item.update(position=saved['position'] if saved else 0, completed=bool(saved['completed']) if saved else False)
        return result
    except KeyError:
        raise HTTPException(404, 'Torrent not found')
    except ValueError as error:
        raise HTTPException(409, str(error))


@app.api_route('/v1/torrents/{key}/files/{index}', methods=['GET', 'HEAD'])
async def media(key: str, index: int, request: Request, uid=Depends(user)):
    try:
        _, _, _, size, _ = app.state.torrents.file(key, index)
    except KeyError:
        raise HTTPException(404, 'Torrent not found')
    except ValueError as error:
        raise HTTPException(409, str(error))
    try:
        start, end, code = byte_range(request.headers.get('range'), size)
    except ValueError:
        return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
    headers = {'Accept-Ranges': 'bytes', 'Content-Length': str(end-start+1)}
    if code == 206:
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    integrity = request.headers.get('x-mcore-integrity')
    if integrity is not None:
        if integrity != 'sha256' or code != 206 or end-start+1 > 4*1024*1024:
            raise HTTPException(400, 'SHA-256 requires a byte range of at most 4 MiB')
        try:
            content = bytearray()
            async for chunk in app.state.torrents.stream(key, index, start, end):
                content.extend(chunk)
            if len(content) != end-start+1:
                raise OSError('Incomplete range')
        except (TimeoutError, OSError):
            raise HTTPException(503, 'Verified range temporarily unavailable')
        headers['X-MCore-SHA256'] = hashlib.sha256(content).hexdigest()
        headers['Cache-Control'] = 'no-store'
        return Response(content=b'' if request.method == 'HEAD' else bytes(content),
                        status_code=code, headers=headers, media_type='application/octet-stream')
    if request.method == 'HEAD':
        return Response(status_code=code, headers=headers, media_type='application/octet-stream')
    return StreamingResponse(app.state.torrents.stream(key, index, start, end),
                             status_code=code, headers=headers, media_type='application/octet-stream')
