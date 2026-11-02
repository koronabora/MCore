"""Import known series from IMDb's official title.episode TSV (plain or gzip).

Usage: python -m mcore.imdb_import /path/title.episode.tsv.gz
Only explicit tracker IMDb IDs are joined; names are never fuzzy-matched here.
"""
import csv
import gzip
import json
import sys
from collections import defaultdict
from .config import load
from .store import Store


def import_episodes(store, path):
    parents = defaultdict(set)
    with store.connect() as db:
        for row in db.execute('SELECT data FROM releases'):
            item = json.loads(row['data'])
            imdb = item.get('external_ids', {}).get('imdb')
            if imdb and item.get('series'):
                parents[imdb].add(item['movie'])
    numbers = defaultdict(set)
    unknown = set()
    episodes = []
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream, delimiter='\t'):
            parent, season, episode = row['parentTconst'], row['seasonNumber'], row['episodeNumber']
            if parent in parents and season.isdigit() and not episode.isdigit(): unknown.add((parent, int(season)))
            if parent in parents and season.isdigit() and episode.isdigit() and 0 < int(episode) <= 1000:
                numbers[parent, int(season)].add(int(episode))
                episodes.append((parent, int(season), int(episode), row['tconst']))
    count = 0
    with store.connect() as db:
        for parent, season, number, external_id in episodes:
            for movie in parents[parent]:
                db.execute('INSERT OR REPLACE INTO external_episodes VALUES (?,?,?,?,?)', (movie, f's{season:03}e{number:04}', external_id, season, number))
        for (parent, season), values in numbers.items():
            # Gaps or unknown-number records must not imply a complete season.
            if (parent, season) in unknown or values != set(range(1, max(values)+1)):
                continue
            for movie in parents[parent]:
                db.execute('INSERT OR REPLACE INTO season_metadata VALUES (?,?,?,?)', (movie, season, max(values), 'imdb:'+parent))
                count += 1
    return count


if __name__ == '__main__':
    print('Imported season totals:', import_episodes(Store(load()['state_dir']), sys.argv[1]))
