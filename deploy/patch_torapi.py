"""Reproducible compatibility patches for the pinned TorAPI version."""
from pathlib import Path
import re
import sys


def patch(path):
    text = path.read_text()
    before, after = 'const server = web.listen(port)', "const server = web.listen(port, '127.0.0.1')"
    if before not in text and after not in text:
        raise SystemExit('TorAPI listener changed: inspect upstream before installation')
    text = text.replace(before, after)
    # Upstream hardcodes the obsolete .tv origin in detail, hash, poster and RSS
    # requests too, so merely reordering its search mirrors is insufficient.
    if 'https://kinozal.tv' not in text and 'https://kinozal.me' not in text:
        raise SystemExit('TorAPI Kinozal origins changed: inspect upstream before installation')
    text = text.replace('https://kinozal.tv', 'https://kinozal.me')
    text = re.sub(r'''(["'])https://kinozal\.me\1,\s*(["'])https://kinozal\.me\2''',
                  r'\1https://kinozal.me\1', text)
    path.write_text(text)


if __name__ == '__main__':
    patch(Path(sys.argv[1]))
