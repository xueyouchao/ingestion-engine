# The bundle's host

`run.sh` puts the exported reader bundle on this VPS behind the Traefik that already serves every other
site here, at `https://pdf-ingestion.srv1567269.hstgr.cloud/`.

- **The site is a container**: `nginx:alpine`, pinned by digest in `run.sh` (`IMAGE=...` overrides it),
  named `ingestion-reader`, serving `dist/bundle` read-only, with `--restart unless-stopped`. Re-run
  `run.sh` to replace it; rebuilding the bundle needs no restart, because the mount is live. The script
  proves the nginx config in a throwaway container before it removes the running one, so a bad config
  cannot take the host down.
- **Routing is Docker labels**, not an edit to `/etc/traefik/dynamic.yml`: the Traefik container mounts
  `/etc/traefik` from a root-owned host directory, and its Docker provider already has
  `exposedbydefault=false`, so `traefik.enable=true` plus a router is enough and needs docker access only.
- **TLS** comes from the existing `letsencrypt` resolver (HTTP-01); Traefik renews it, nothing here does.
- **`nginx.conf`** adds gzip (the page is ~3.8 MiB of inline manifest, ~1.2 MiB compressed), declares
  `charset utf-8` (the OCR text is not ASCII), sends `nosniff`, and caches `images/` as immutable while
  always revalidating `index.html`. That cache is only safe because a render's file name carries the hash
  of its own bytes: a different dpi is a different URL.
- **The page is public.** No auth: the link is the demo. Every declared fact on it is already public
  corpus material.

Regenerate and republish:

```bash
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli bundle \
  --out dist/bundle --state paddleocr-vl=.demo-gpu/out --state pymupdf=.demo/out \
  --corpus .demo-gpu/corpus --link https://pdf-ingestion.srv1567269.hstgr.cloud/ \
  --live /live/
./deploy/bundle/run.sh   # only when the container itself needs replacing
```

`--live` is what puts the header link to the demo on the page, and it is a build argument because the
same page opens from `file://` and from hosts that serve no demo. This deployment serves `/live/`, so the
rebuild passes `/live/` as a relative URL: the page resolves it against whatever origin it was served
from, and a bundle built without the flag simply has no link.

`--link` is recorded in `bundle.json` and printed by the page whenever a page image cannot load: a copy
opened in a preview pane (or any context that cannot read the sibling `images/` folder) then says where the
served copy is instead of rendering a broken-image icon. Opening `dist/bundle/index.html` from a real
browser on disk works too — `file://` can read sibling files; a sandboxed preview cannot.
