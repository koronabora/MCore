"""Run as root once: undo Sonarr-specific removal of Russian title letters."""
import json
from pathlib import Path
import shutil
import subprocess

path = Path('/var/lib/mcore-indexers/jackett/Jackett/Indexers/rutracker.json')
subprocess.run(['docker', 'stop', 'mcore-jackett'], check=True)
try:
    backup = path.with_suffix('.json.before-mcore-020')
    if not backup.exists():
        shutil.copy2(path, backup)
    values = json.loads(path.read_text())
    for item in values:
        if item['id'] == 'striprussianletters':
            item['value'] = False
    path.write_text(json.dumps(values, ensure_ascii=False, indent=2))
finally:
    subprocess.run(['docker', 'start', 'mcore-jackett'], check=True)
