#!/usr/bin/env bash
# Serve the exported reader bundle on this VPS behind the existing Traefik.
#
# The container is the site: nginx serves `dist/bundle` read-only, Traefik routes the host to it and
# issues the certificate with the `letsencrypt` resolver that every other site here already uses. Nothing
# in /etc/traefik changes, so this needs docker access only. Re-running replaces the container in place,
# and rebuilding the bundle needs no restart at all -- the mount is read-only and live.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME="${NAME:-ingestion-reader}"
HOST="${HOST:-pdf-ingestion.srv1567269.hstgr.cloud}"
# Pinned by digest: the site is one container of static files, so the image should not drift under it.
IMAGE="${IMAGE:-nginx:alpine@sha256:892bb2152464f0c5d8972593a5f5afaf8054552580b699d95f8ff4af08c2d33d}"

if [ ! -f "$ROOT/dist/bundle/index.html" ]; then
  echo "no bundle at $ROOT/dist/bundle; build it first:" >&2
  echo "  PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli bundle \\" >&2
  echo "    --out dist/bundle --state paddleocr-vl=.demo-gpu/out --state pymupdf=.demo/out \\" >&2
  echo "    --corpus .demo-gpu/corpus \\" >&2
  echo "    --link https://$HOST/ \\" >&2
  echo "    --live /live/" >&2
  exit 1
fi

# Prove the config before taking the site down: a rejected config in a throwaway container costs nothing,
# while starting the real one with it would leave the host unrouted.
# ``--add-host`` here too: the config names host.docker.internal for the live demo's upstream, and nginx
# refuses to load a config whose upstream name it cannot resolve.
docker run --rm --add-host=host.docker.internal:host-gateway \
  -v "$ROOT/deploy/bundle/nginx.conf:/etc/nginx/conf.d/default.conf:ro" "$IMAGE" nginx -t >/dev/null

docker rm -f "$NAME" >/dev/null 2>&1 || true
# --add-host: the live demo answers on the docker bridge (deploy/demo/), and this name is how the
# container's nginx reaches it. A comment inside the continued command would swallow the rest of it.
docker run -d --name "$NAME" --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -v "$ROOT/dist/bundle:/usr/share/nginx/html:ro" \
  -v "$ROOT/deploy/bundle/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  --label traefik.enable=true \
  --label "traefik.http.routers.$NAME.rule=Host(\`$HOST\`)" \
  --label "traefik.http.routers.$NAME.entrypoints=websecure" \
  --label "traefik.http.routers.$NAME.tls=true" \
  --label "traefik.http.routers.$NAME.tls.certresolver=letsencrypt" \
  --label "traefik.http.routers.$NAME.service=$NAME" \
  --label "traefik.http.services.$NAME.loadbalancer.server.port=80" \
  "$IMAGE" >/dev/null

sleep 2
if ! docker exec "$NAME" nginx -t >/dev/null 2>&1; then
  echo "the new container rejected its config; the previous one is gone, so rerun this script" >&2
  exit 1
fi
echo "serving $ROOT/dist/bundle at https://$HOST/"
