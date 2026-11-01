"""Root-only: configure the initialized trial instance without printing secrets."""
import json
from pathlib import Path
import shutil
import subprocess

path = Path('/var/lib/mcore-indexers/jackett/Jackett/ServerConfig.json')
if not path.exists():
    raise SystemExit('Start tracker-trial.sh and wait for Jackett initialization first')
subprocess.run(['docker', 'stop', 'mcore-jackett'], check=True)
try:
    backup = path.with_suffix('.json.before-flaresolverr')
    if not backup.exists():
        shutil.copy2(path, backup)
    data = json.loads(path.read_text())
    data.update(FlareSolverrUrl='http://mcore-flaresolverr:8191',
                FlareSolverrMaxTimeout=90000, UpdateDisabled=True)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)
finally:
    subprocess.run(['docker', 'start', 'mcore-jackett'], check=True)
print('Jackett configured to use FlareSolverr; updates pinned to the container image')
