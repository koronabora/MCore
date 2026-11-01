"""Stable episode identities across releases. Ambiguous files stay release-specific."""
import re


def identify(name, release):
    normalized = name.replace('\\', '/')
    if re.search(r'(?i)S\d+[ ._-]*E\d+(?:[ ._-]+E?\d+|E\d+)', normalized):
        return '', None, None
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
    return '', None, None


def register(store, release, torrent, files):
    if not release.get('series'):
        return
    with store.connect() as db:
        for file in files:
            episode, season, number = identify(file['name'], release)
            episode = episode or f"file:{torrent}:{file['index']}"
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
