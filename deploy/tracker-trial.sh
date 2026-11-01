#!/bin/sh
# Root-only trial stack. Persistent account state stays outside git.
set -eu
id mcore-indexers >/dev/null 2>&1 || useradd --system --home-dir /var/lib/mcore-indexers --shell /usr/sbin/nologin mcore-indexers
install -d -m 0700 -o mcore-indexers -g mcore-indexers /var/lib/mcore-indexers /var/lib/mcore-indexers/jackett
docker network inspect mcore-indexers >/dev/null 2>&1 || docker network create mcore-indexers
docker inspect mcore-flaresolverr >/dev/null 2>&1 || docker run -d \
  --name mcore-flaresolverr --network mcore-indexers --restart unless-stopped \
  -p 127.0.0.1:8191:8191 --shm-size=512m --memory=2g \
  --log-opt max-size=10m --log-opt max-file=2 -e LOG_LEVEL=info -e LOG_HTML=false \
  ghcr.io/flaresolverr/flaresolverr@sha256:c80ae007ce2ccdcd217a12426e4f039ef763ff90738c808d38810c3e59323767
docker inspect mcore-jackett >/dev/null 2>&1 || docker run -d \
  --name mcore-jackett --network mcore-indexers --restart unless-stopped \
  -p 127.0.0.1:9117:9117 --memory=1g \
  --log-opt max-size=10m --log-opt max-file=2 \
  -e PUID="$(id -u mcore-indexers)" -e PGID="$(id -g mcore-indexers)" \
  -e TZ=Etc/UTC -e AUTO_UPDATE=false -e UMASK=077 \
  -v /var/lib/mcore-indexers/jackett:/config \
  lscr.io/linuxserver/jackett@sha256:f4032d1272c1c9ca7f286b40cd1ecd02f3df24e9fe69b36ae4b62c4cfa69541a
