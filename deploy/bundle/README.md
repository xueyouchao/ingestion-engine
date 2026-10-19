# The bundle's host

`run.sh` puts the exported reader bundle on this VPS behind the Traefik that already serves every other
site here, at `https://pdf-ingestion.srv1567269.hstgr.cloud/`.

- **The site is a container**: `nginx:alpine` named `ingestion-reader`, serving `dist/bundle` read-only,
  with `--restart unless-stopped`. Re-run `run.sh` to replace it; rebuilding the bundle needs no restart.
- **Routing is Docker labels**, not an edit to `/etc/traefik/dynamic.yml`: the Traefik container mounts
  `/etc/traefik` from a root-owned host directory, and its Docker provider already has
  `exposedbydefault=false`, so `traefik.enable=true` plus a router is enough and needs docker access only.
- **TLS** comes from the existing `letsencrypt` resolver (HTTP-01); Traefik renews it, nothing here does.
- **`nginx.conf`** adds gzip (the page is ~3.8 MiB of inline manifest, ~1.2 MiB compressed) and caches
  `images/` as immutable while always revalidating `index.html`.
- **The page is public.** No auth: the link is the demo. Every declared fact on it is already public
  corpus material.

Regenerate and republish:

```bash
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli bundle \
  --out dist/bundle --state paddleocr-vl=.demo-gpu/out --state pymupdf=.demo/out \
  --corpus .demo-gpu/corpus --link https://pdf-ingestion.srv1567269.hstgr.cloud/
./deploy/bundle/run.sh   # only when the container itself needs replacing
```

`--link` is recorded in `bundle.json` and printed by the page whenever a page image cannot load: a copy
opened in a preview pane (or any context that cannot read the sibling `images/` folder) then says where the
served copy is instead of rendering a broken-image icon. Opening `dist/bundle/index.html` from a real
browser on disk works too — `file://` can read sibling files; a sandboxed preview cannot.
