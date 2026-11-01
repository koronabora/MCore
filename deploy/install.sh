#!/bin/sh
set -eu
# Run as root from this repository. Credentials/runtime files never enter the repo.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
id mcore >/dev/null 2>&1 || useradd --system --home-dir /var/lib/mcore --shell /usr/sbin/nologin mcore
install -d -m 0755 /opt/mcore/app /opt/mcore/torapi
install -d -m 0750 -o root -g mcore /etc/mcore
install -d -m 0700 -o mcore -g mcore /var/lib/mcore /var/cache/mcore
cp -R "$ROOT/mcore" /opt/mcore/app/
if [ ! -f /etc/mcore/config.toml ]; then
    install -m 0640 -o root -g mcore "$ROOT/config.example.toml" /etc/mcore/config.toml
fi
if [ ! -f /etc/mcore/tls.key ]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 365 \
        -keyout /etc/mcore/tls.key -out /etc/mcore/tls.crt -subj '/CN=MCore local development' \
        -addext 'subjectAltName=IP:10.16.16.31,IP:127.0.0.1,DNS:localhost'
fi
chown root:mcore /etc/mcore/tls.key /etc/mcore/tls.crt
chmod 0640 /etc/mcore/tls.key /etc/mcore/tls.crt
python3 -m venv --system-site-packages /opt/mcore/venv
/opt/mcore/venv/bin/pip install --disable-pip-version-check -q -r "$ROOT/requirements.txt"
cd /opt/mcore/torapi
npm install --no-progress --save-exact torapi@0.5.2 cheerio@1.0.0
# Upstream has no bind-address option. Pin the single listener to loopback.
python3 - <<'PY'
from pathlib import Path
p = Path('node_modules/torapi/main.js')
s = p.read_text()
before, after = 'const server = web.listen(port)', "const server = web.listen(port, '127.0.0.1')"
if before not in s and after not in s:
    raise SystemExit('TorAPI listener changed: inspect upstream before installation')
p.write_text(s.replace(before, after))
PY
install -m 0644 "$ROOT/deploy/mcore.service" "$ROOT/deploy/mcore-torapi.service" /etc/systemd/system/
install -m 0755 "$ROOT/deploy/mcore-admin" /usr/local/sbin/mcore-admin
systemctl daemon-reload
systemctl enable mcore.service mcore-torapi.service
systemctl restart mcore-torapi.service mcore.service
