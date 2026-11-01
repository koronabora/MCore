import os
from pathlib import Path
import tomllib
from .network import validate_torrent


def load():
    path = os.environ.get('MCORE_CONFIG', '/etc/mcore/config.toml')
    with open(path, 'rb') as f:
        config = tomllib.load(f)
    if not 40000 < config['port'] < 65536:
        raise ValueError('API port must be above 40000')
    if config['sync_seconds'] < 60 or config['cache_bytes'] < 1048576:
        raise ValueError('Invalid sync interval or cache capacity')
    if not config['public_url'].startswith('https://'):
        raise ValueError('HTTPS required')
    config['torrent'] = validate_torrent(config.get('torrent', {}), config['port'])
    providers = config.get('providers', ['rutracker', 'kinozal'])
    if not isinstance(providers, list) or any(p not in ('rutracker', 'kinozal') for p in providers):
        raise ValueError('Supported providers: rutracker, kinozal')
    config['providers'] = list(dict.fromkeys(providers))
    config.setdefault('archive_enabled', True)
    config.setdefault('sync_request_delay', 3)
    if type(config['archive_enabled']) is not bool or not 1 <= config['sync_request_delay'] <= 300:
        raise ValueError('Invalid archive or synchronization settings')
    Path(config['state_dir']).mkdir(parents=True, exist_ok=True, mode=0o700)
    return config
