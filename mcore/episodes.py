"""Stable episode identities across releases. Ambiguous files stay release-specific."""
import re
import json


def identify(name, release):
    normalized = name.replace('\\', '/')
    film = re.search(r'(?i)(?:фильм|film)\s*[._ -]*(\d{1,3})', normalized)
    part = re.search(r'(?i)(?:часть|part|серия)\s*[._ -]*(\d{1,3})', normalized)
    if release.get('cycle') and film:
        number = int(part[1]) if part else 1
        return f'film{int(film[1]):03}part{number:03}', int(film[1]), number
    if re.search(r'(?i)S\d+[ ._-]*E\d+(?:[ ._-]+E?\d+|E\d+)', normalized):
        return '', None, None
    # Some releases use ep.-06x13 for episode 6 of 13, not season 6/episode 13.
    numbered = re.search(r'(?i)\bep[._ -]+(\d{1,4})x(\d{1,4})(?!\d)', normalized.rsplit('/', 1)[-1])
    if numbered:
        ordinal = re.search(r'(?i)\b(\d{1,3})(?:st|nd|rd|th)[ ._-]+season\b', normalized)
        season_value = ordinal[1] if ordinal else release.get('season', '')
        if str(season_value).isdigit() and 0 < int(numbered[1]) <= int(numbered[2]):
            season, number = int(season_value), int(numbered[1])
            return f's{season:03}e{number:04}', season, number
    match = re.search(r'(?i)\bS(\d{1,3})[ ._-]*E(\d{1,4})(?!\d|[ ._-]*E\d)', normalized)
    if not match:
        match = re.search(r'(?i)\b(\d{1,3})x(\d{1,4})(?!\d)', normalized)
    if match:
        season, episode = map(int, match.groups())
        return f's{season:03}e{episode:04}', season, episode
    season = re.search(r'(?i)(?:сезон|season)[ ._-]*(\d{1,3})|(\d{1,3})[ ._-]*сезон', normalized)
    value = next((x for x in season.groups() if x), '') if season else release.get('season', '')
    episode = re.search(r'(?i)(?:серия|эпизод|episode|\bE)[ ._-]*(\d{1,4})|(\d{1,4})[ ._-]*(?:серия|эпизод)', normalized)
    if not episode:
        episode = re.search(r'^(\d{1,3})[ ._-]', normalized.rsplit('/', 1)[-1])
    if str(value).isdigit() and episode:
        number = int(next(x for x in episode.groups() if x))
        return f's{int(value):03}e{number:04}', int(value), number
    # Explicit episode labels identify a series even when the tracker omitted its season.
    # Plain numeric filenames alone are not evidence of episodes.
    explicit = re.search(r'(?i)(?:серия|эпизод|episode)[ ._-]*(\d{1,4})(?!\d)', normalized.rsplit('/', 1)[-1])
    if explicit and not value:
        number = int(explicit[1])
        return f's000e{number:04}', 0, number
    return '', None, None


def register(store, release, torrent, files):
    with store.connect() as db:
        db.execute('DELETE FROM source_files WHERE release=?', (release['id'],))
        for file in files:
            episode, season, number = identify(file['name'], release)
            if not release.get('series') and not episode:
                episode = 'movie' if len(files) == 1 else ''
            episode = episode or f"file:{torrent}:{file['index']}"
            data = dict(file, episode=episode, season=season, number=number, torrent=torrent, release=release['id'])
            db.execute('INSERT OR REPLACE INTO source_files VALUES (?,?,?)', (release['id'], file['index'], json.dumps(data)))
            db.execute('INSERT OR REPLACE INTO episode_files VALUES (?,?,?,?,?,?,?,?)',
                       (release['id'], torrent, file['index'], release['movie'], episode, season, number, file['name']))


def listing(store, aliases, uid):
    placeholders = ','.join('?' for _ in aliases)
    with store.connect() as db:
        files = db.execute(f'SELECT * FROM episode_files WHERE movie IN ({placeholders}) ORDER BY season,number,name', aliases).fetchall()
        history = db.execute(f'SELECT * FROM episode_history WHERE user=? AND movie IN ({placeholders}) ORDER BY updated', (uid, *aliases)).fetchall()
    progress = {row['episode']: dict(row) for row in history}
    groups = {}
    for row in files:
        key = row['episode']
        item = groups.setdefault(key, {'id': key, 'season': row['season'], 'number': row['number'], 'name': row['name'], 'sources': [], 'position': 0, 'completed': False})
        item['sources'].append({k: row[k] for k in ('release', 'torrent', 'file', 'name')})
        if key in progress:
            item.update({k: progress[key][k] for k in ('position', 'duration', 'completed')})
    return list(groups.values())


def catalog(store, releases, aliases, uid):
    """Works first; source lists are built only from inspected torrent manifests."""
    by_release = {r['id']: r for r in releases}
    placeholders = ','.join('?' for _ in by_release) or 'NULL'
    movie_placeholders = ','.join('?' for _ in aliases) or 'NULL'
    with store.connect() as db:
        rows = db.execute(f'SELECT release,data FROM source_files WHERE release IN ({placeholders})', tuple(by_release)).fetchall()
        history = db.execute(f'SELECT * FROM episode_history WHERE user=? AND movie IN ({movie_placeholders}) ORDER BY updated', (uid, *aliases)).fetchall()
        totals = {r['season']: r['total'] for r in db.execute(f'SELECT * FROM season_metadata WHERE movie IN ({movie_placeholders})', aliases)}
        jobs = {r['release']: dict(r) for r in db.execute(f'SELECT * FROM source_index_jobs WHERE release IN ({placeholders})', tuple(by_release))}
        external = db.execute(f'SELECT * FROM external_episodes WHERE movie IN ({movie_placeholders})', aliases).fetchall()
    groups, manifests = {}, {}
    progress = {r['episode']: dict(r) for r in history if r['movie'] in aliases}
    latest = max(progress.values(), key=lambda r: r['updated'], default={}).get('episode', '')
    for row in external:
        key = row['episode']
        groups[key] = dict(id=key, season=row['season'], number=row['number'], name=f"Серия {row['number']}", external_id=row['external_id'], sources=[], position=0, completed=False, last_watched=key == latest)
        if key in progress: groups[key].update({k: progress[key][k] for k in ('position', 'duration', 'completed')})
    for row in rows:
        if row['release'] not in by_release:
            continue
        file = json.loads(row['data'])
        # Reinterpret cached manifests as well as newly indexed torrents.
        identity, season, number = identify(file['name'], by_release[row['release']])
        if identity:
            file.update(episode=identity, season=season, number=number)
            by_release[row['release']]['series'] = True
        manifests.setdefault(row['release'], []).append(file)
        key = file['episode']
        item = groups.setdefault(key, dict(id=key, season=file['season'], number=file['number'], name=file['name'], sources=[], position=0, completed=False, last_watched=key == latest))
        item['sources'].append(file)
        if key in progress:
            item.update({k: progress[key][k] for k in ('position', 'duration', 'completed')})
    # An explicit "episodes 1-N of N" in a single-season release is evidence of the total.
    for release in releases:
        season = str(release.get('season', ''))
        match = re.search(r'(?i)(?:сери[ияй]|episodes?)\s*[: ]*1\s*[-–]\s*(\d+)\s*(?:из|of)\s*(\d+)', release.get('release_title', ''))
        if season.isdigit() and match and match[1] == match[2] and int(match[2]) <= 1000:
            totals[int(season)] = max(totals.get(int(season), 0), int(match[2]))
        # A complete multi-season pack must prove both its declared total and
        # contiguous episode numbering in every declared season. Duplicate cuts
        # of one episode do not count as additional episodes.
        span = re.fullmatch(r'(\d+)\s*[-–]\s*(\d+)', season)
        if span and match and match[1] == match[2] and 0 < int(match[2]) <= 1000:
            first, last = map(int, span.groups())
            files = manifests.get(release['id'], [])
            numbers = {}
            for f in files:
                if f['season'] is not None and f['number'] is not None:
                    numbers.setdefault(f['season'], set()).add(f['number'])
            if (0 < first <= last <= 1000 and set(numbers) == set(range(first, last+1))
                    and sum(map(len, numbers.values())) == int(match[2])
                    and all(ns == set(range(1, len(ns)+1)) for ns in numbers.values())
                    and all(f['season'] is not None and f['number'] is not None for f in files)):
                for n, ns in numbers.items():
                    totals.setdefault(n, len(ns))
    complete = {}
    for rid, files in manifests.items():
        complete[rid] = [season for season, count in totals.items() if count > 0 and
                         {f['number'] for f in files if f['season'] == season and f['number'] is not None} >= set(range(1, count+1))]
    return dict(works=sorted(groups.values(), key=lambda x: (x['season'] if x['season'] is not None else 9999, x['number'] if x['number'] is not None else 9999, x['name'])),
                complete_seasons=complete, season_totals=totals,
                indexed_releases=list(manifests), indexing=any(not j['completed'] and j['attempts'] < 3 for j in jobs.values()),
                index_errors={rid: j['error'] for rid, j in jobs.items() if j['error']})
