# The live demo, beside the bundle

Two things share `https://pdf-ingestion.srv1567269.hstgr.cloud/`:

| Path | What answers it |
| --- | --- |
| `/` and `/images/` | the exported reader bundle, served statically by the `ingestion-reader` container (`deploy/bundle/`) |
| `/live/` and `/api/…` | the live upload demo (ADR-0012), served by `finepdfs-live-demo` on this host |

The bundle's page links to `/live/`, and the demo page's own fetches are absolute (`/api/status`,
`/api/read`), so one proxy in front of both is all the merge needed: the container's nginx forwards those
two paths to the demo, which then answers on the same origin and no CORS, no extra certificate and no
second host are involved.

## What runs

- **The demo** is a systemd unit, `finepdfs-live-demo.service` (this directory). It binds `172.17.0.1:8766`
  -- the docker bridge -- so the container reaches it as `host.docker.internal` and the public internet
  cannot: every visitor arrives through the nginx that sets `X-Forwarded-For`, which is the address the
  rate limit counts (it is a spend brake, not an identity). Install it, and restart it after a change:

  ```bash
  sudo cp deploy/demo/finepdfs-live-demo.service /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now finepdfs-live-demo
  sudo systemctl status finepdfs-live-demo --no-pager
  journalctl -u finepdfs-live-demo -n 50 --no-pager
  ```

- **The OCR lane runs this host's engine**, not the deployed model: `FINEPDFS_ENGINE_COMMAND` points at
  `deploy/demo/ocr-shim`, which speaks the CLI engine's `doc_parser` contract over **tesseract**. That is
  why a scan reads at all on a GPU-less VPS, and why the Engine Identity on the page names the shim -- a
  reader can see whose text they are looking at. When the Modal worker app is deployed
  (`deploy/modal/serve.py`, ADR-0012), replace those two `Environment=` lines with
  `FINEPDFS_ENGINE_BASE_URL` and `FINEPDFS_ENGINE_API_KEY` and drop the shim.

- **The proxy** is four lines in `deploy/bundle/nginx.conf` (`location /live/`, `location /api/`), and the
  container needs `--add-host=host.docker.internal:host-gateway` so those names resolve; `run.sh` already
  passes it. Replacing the container is the only step that needs docker.

The TLS certificate, the host rule and the trailing `Host(`…`)` router are the bundle's, unchanged:
Traefik still routes the whole host to one container, and the container decides which path is which.

## Checking it

```bash
curl -fsS https://pdf-ingestion.srv1567269.hstgr.cloud/            | grep -o 'href="/live/"'
curl -fsS https://pdf-ingestion.srv1567269.hstgr.cloud/api/status  | head -c 200
curl -fsS -X POST -H 'Content-Type: image/png' \
  --data-binary @tests/fixtures/scan.png \
  https://pdf-ingestion.srv1567269.hstgr.cloud/api/read            # -> {"job": "..."}
```
