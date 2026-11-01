import os
from pathlib import Path
import tomllib


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
    Path(config['state_dir']).mkdir(parents=True, exist_ok=True, mode=0o700)
    return config
