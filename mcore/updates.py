"""Atomic release manifest and immutable, content-addressed APKs."""
import json
import re
from pathlib import Path
from fastapi import HTTPException


def directory(state_dir):
    return Path(state_dir)/'app-updates'


def latest(state_dir):
    root = directory(state_dir)
    try:
        release = json.loads((root/'latest.json').read_text())
    except FileNotFoundError:
        return {'version_code': 0}
    if (type(release.get('version_code')) is not int or release['version_code'] <= 0
            or not isinstance(release.get('version_name'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', release.get('sha256', ''))
            or type(release.get('size')) is not int or not 0 < release['size'] <= 200*1024**2):
        raise HTTPException(503, 'Invalid release manifest')
    apk = root/(release['sha256']+'.apk')
    if not apk.is_file() or apk.stat().st_size != release['size']:
        raise HTTPException(503, 'Release APK not ready')
    return {k: release.get(k, '') for k in ('version_code', 'version_name', 'sha256', 'size', 'notes')}


def artifact(state_dir, digest):
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise HTTPException(404)
    path = directory(state_dir)/(digest+'.apk')
    if not path.is_file():
        raise HTTPException(404)
    return path
