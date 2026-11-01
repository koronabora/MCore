"""Root-only: install a private API key and enable the verified Jackett adapter."""
import grp
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

state = Path('/var/lib/mcore-indexers/jackett/Jackett')
config = Path('/etc/mcore/config.toml')
text = config.read_text()
if 'jackett' in tomllib.loads(text):
    raise SystemExit('Jackett is already configured; inspect existing configuration')
key = json.loads((state / 'ServerConfig.json').read_text())['APIKey']
keyfile = Path('/etc/mcore/jackett.key')
fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
with os.fdopen(fd, 'w') as stream:
    stream.write(key + '\n')
os.chown(keyfile, 0, grp.getgrnam('mcore').gr_gid)
shutil.copy2(config, config.with_suffix('.toml.before-jackett'))
text += '\n[jackett]\nurl = "http://127.0.0.1:9117"\napi_key_file = "/etc/mcore/jackett.key"\n'
tomllib.loads(text)
config.write_text(text)
# Preserve Russian titles instead of applying Sonarr-specific title rewriting.
subprocess.run(['docker', 'stop', 'mcore-jackett'], check=True)
try:
    indexer = state / 'Indexers/kinozal-magnet.json'
    shutil.copy2(indexer, indexer.with_suffix('.json.before-title-settings'))
    values = json.loads(indexer.read_text())
    for value in values:
        if value['id'] == 'stripcyrillicletters' or value['id'].startswith('addrussiantoendofalltitles'):
            value['value'] = False
    indexer.write_text(json.dumps(values, ensure_ascii=False, indent=2))
finally:
    subprocess.run(['docker', 'start', 'mcore-jackett'], check=True)
print('Jackett enabled; key stored outside the repository')
