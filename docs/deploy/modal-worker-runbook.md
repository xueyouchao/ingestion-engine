# Modal GPU worker runbook

The offline half of ADR-0009: rent one Modal sandbox that serves PaddleOCR-VL behind `/v1`, wire it
through the existing `command` provider, and give it back. **No Modal resource was created while this
slice was written, and no number below is a measured run.** It is a procedure with checks, not evidence.

The commands name how to check machine state; they do not claim what the answer will be.

## Lifecycle choice: one detached Sandbox, not `modal deploy`

`deploy/modal/start.py` looks up (or creates) the empty app `finepdfs-gpu-worker`, then creates **one
detached `modal.Sandbox`** from the pinned worker image, with a GPU, an encrypted tunnel on port 8000,
a TCP readiness probe and a lifetime cap. The Sandbox outlives the start process, so the command exits
after readiness and `CommandGpuProvider` is not deadlocked. `deploy/modal/stop.py` reads the sandbox id
from the lease file and calls `Sandbox.from_id(id).terminate()`.

**One lease, one state file.** `CommandGpuProvider` mints an identity per lease and puts it in the
environment of that lease's start and stop command as `FINEPDFS_GPU_LEASE_ID`; the scripts record the
lease at `$FINEPDFS_GPU_STATE_DIR/<identity>.json`. Two runs therefore record two workers and each can
only give back its own -- a single shared file used to make the second run refuse to start, and then its
stop terminated the first run's live worker. A `start` you run by hand has no provider to mint an
identity and records under `manual.json`, so `start` and then `stop` from one shell still meet.

Why a Sandbox rather than a deployed app or function: the lease is per run, the SDK terminates it by
id from another process, and there is no deployment left serving (and billing) between runs. A deployed
app would need `modal deploy`, a function URL and a second lifecycle for concurrency; ADR-0009 gives the
worker exactly one run's lifetime. The SDK source was inspected offline (`modal==1.5.5`), not executed.

RunPod later is another `start.py`/`stop.py` pair writing the same lease and JSON payload through the
same `command` provider. Nothing in the flow, engines or `PROVIDERS` changes.

## Files

| Path | Role |
| --- | --- |
| `deploy/gpu/contract.py` | endpoint JSON, `/v1` validation, `model@version` detail without a URL, one atomic lease file per lease, redaction of credentials and addresses |
| `deploy/gpu/server.py` | in-container launcher for the pinned vLLM OpenAI server (`/v1/models`, `/v1/chat/completions`) |
| `deploy/modal/image.py` | pinned vLLM image + launcher entrypoint, weights revision and served name as env, HF cache volume path |
| `deploy/modal/start.py` | creates one Sandbox, writes the lease, waits for `/v1/models`, prints one JSON object |
| `deploy/modal/stop.py` | terminates the recorded Sandbox; idempotent and tolerant of a half-started worker |
| `deploy/requirements-modal.txt` | deploy environment, `modal==1.5.5` |

## Budget gate — do not skip

Budget is **not confirmed**. Do not run a start command until all of these are true:

1. The person who owns the Modal workspace has approved a spend cap and a maximum wall time in writing.
2. The current rates are recorded from `modal billing rates` (GPU per second, plus CPU/memory), and the
   approved cap is at least the expected cost of the planned run.
3. `FINEPDFS_GPU_WORKER_LIFETIME` is set to that maximum wall time. It is the hard backstop: Modal
   terminates the Sandbox at that age even if `stop` never runs. The default is 3600 seconds.
4. The next gate is the one-page smoke below, never the full corpus.
5. `deploy/modal/stop.py` is run and `modal container list` is checked after every attempt, successful
   or not. A leaked GPU bills by the hour (ADR-0007).

The worker downloads the pinned weights on the first start and caches them in the workspace Volume
`finepdfs-hf-cache` (mounted at `/root/.cache/huggingface`), so later runs do not pay that download
again. The first run's wall time includes it; no model weights are committed or shipped here.

## Preflight

From the repository root, in a deploy environment built from the pinned requirements, not the base
package:

```bash
python -m venv .venv-deploy
. .venv-deploy/bin/activate
python -m pip install -r deploy/requirements-modal.txt

modal --version                  # the requirements pin 1.5.5; anything else is a changed environment
modal profile current            # the profile that will be billed
modal token info                 # authenticated? names token/workspace, never prints the secret
modal billing rates              # record the GPU rate before approving a cap
python -c "import modal; print(modal.__version__)"   # the SDK the scripts will import
```

The core suite does not need Modal and must still pass without it:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

What the image will run, printed without starting the model (the image sets these four variables; the
same values are pinned in `deploy/gpu/contract.py`):

```bash
PADDLEOCR_VL_MODEL=PaddlePaddle/PaddleOCR-VL-1.6 \
PADDLEOCR_VL_REVISION=c5630abae1d940eafe0697512a0325494b02ab42 \
PADDLEOCR_VL_SERVED_MODEL_NAME=PaddleOCR-VL-1.6 \
PADDLEOCR_VL_PORT=8000 \
python -m deploy.gpu.server --print-command
```

## Start and stop

`start` prints exactly one JSON object and exits; `stop` is idempotent. Both write diagnostics to
stderr only. Failure exits nonzero, which the provider reports as an operator problem (the CLI exits 2).

```bash
export FINEPDFS_GPU_STATE_DIR="$PWD/.gpu-worker-leases"   # one file per lease, never committed

# Worker check only: rent, print the endpoint, then give it back.
ENDPOINT="$(python -m deploy.modal.start)"
printf '%s\n' "$ENDPOINT" | python -m json.tool

BASE_URL="$(printf '%s' "$ENDPOINT" | python -c 'import json,sys; print(json.load(sys.stdin)["base_url"])')"
curl -fsS "$BASE_URL/models"
python -m deploy.modal.stop
python -m deploy.modal.stop        # second run must say there is nothing to stop
```

A hand-run `start` has no lease identity and records `manual.json`; the matching `stop` reads the same
file. `base_url` always ends at `/v1`; `detail` is `model@version:weights-<digest>:image-<digest>`, names
the weights revision and image digest and never the address. `start` refuses to overwrite an existing
lease: run `stop` first, so a previous worker is never orphaned. If a start fails after creating the
worker, the id is already on disk and `stop` is the next command.

The tunnel URL is the credential: both scripts mask any URL before they print it (the scheme stays, the
host does not), so an error names what failed and never the address. The one place the address appears is the endpoint JSON on
stdout, which the provider consumes -- keep it out of notes, issues and PRs.

Wire the provider for a pipeline run. The provider runs both commands itself and gives each lease its
own identity in their environment, so nothing shared has to be exported for the hand-off. The
interpreter is the deploy environment, not the base `.venv`, because Modal is not a base dependency:

```bash
export FINEPDFS_GPU_PROVIDER=command
export FINEPDFS_GPU_START_COMMAND="$PWD/.venv-deploy/bin/python -m deploy.modal.start"
export FINEPDFS_GPU_STOP_COMMAND="$PWD/.venv-deploy/bin/python -m deploy.modal.stop"
export FINEPDFS_GPU_WORKER_LIFETIME=1800      # seconds; the budget gate decides this number
export FINEPDFS_GPU_TYPE=L4                   # the GPU the approved rate was quoted for
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `FINEPDFS_GPU_STATE_DIR` | `~/.cache/finepdfs/gpu-workers` | directory holding one state file per lease; never committed |
| `FINEPDFS_GPU_LEASE_ID` | unset (`manual`) | the lease identity; the provider sets it and passes the same value to start and stop |
| `FINEPDFS_GPU_TYPE` | `L4` | Modal GPU string; override only with an approved rate |
| `FINEPDFS_GPU_READY_TIMEOUT` | `900` | seconds to wait for the readiness probe |
| `FINEPDFS_GPU_WORKER_LIFETIME` | `3600` | Sandbox maximum age, the leak backstop |

## Gate 1 — one-page smoke

Purpose: prove the worker serves PaddleOCR-VL, the endpoint is reachable through the tunnel, and one
genuine image-only page produces non-empty accepted text. Cheapest evidence first.

1. Confirm the page really is image-only from evidence that already exists: its declared record
   (`routing_decision: "ocr"`) or a prior CPU Docling dry run. A text-layer page proves nothing, and a
   dry run configured with the rented engine leases a GPU, so it is not the cheap pre-rent check.

2. Run the raw worker check above (`start`, `curl .../v1/models`, then `stop`). Save the JSON and the
   `/v1/models` answer as evidence, with the URL itself redacted from any note or PR.

3. Run the same one page through the pipeline, which leases and releases its own worker:

   ```bash
   PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli run \
     --corpus "$SMOKE_CORPUS" --out "$SMOKE_OUT" \
     --engine paddleocr-vl --model PaddleOCR-VL-1.6 \
     --gpu-provider command
   ```

   Pass criteria: exit 0, one page in `out/records/`, non-empty Markdown, `engine` names
   `paddleocr-vl@...@dpi200` with no Modal host in it. With two pages, page 1 and page 2 stay distinct;
   the 2-page scan is the page-mapping gate, not this one.

4. Check teardown before doing anything else, and record the `out/run-summary.json` line.

## Gate 2 — full pinned NOAA corpus

Purpose: the actual incremental gate, not more smoke. Cold run with real extraction calls, then the
same command again with a new lease and zero extraction calls.

```bash
ROOT=.demo

PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo restore --root "$ROOT"

# cold (budget gate applies; this is the run that costs the most)
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli run \
  --corpus "$ROOT/corpus" --out "$ROOT/out" --search \
  --engine paddleocr-vl --model PaddleOCR-VL-1.6 \
  --gpu-provider command

# warm: the same command, a fresh worker, no new extraction
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli run \
  --corpus "$ROOT/corpus" --out "$ROOT/out" --search \
  --engine paddleocr-vl --model PaddleOCR-VL-1.6 \
  --gpu-provider command
```

Pass criteria, read from the declared summary rather than from prose:

```bash
python -m json.tool "$ROOT/out/run-summary.json"
```

* cold run: `routing.ocr` is the expected number of image-only documents and `extraction.calls` is
  non-zero; `published.documents` covers both routed branches;
* warm run: `extraction.calls` is 0 and `extraction.reused` equals the cold run's calls;
* the Engine Identity in every record and in the memo-bearing detail is unchanged between the runs and
  contains no `://`, no hostname and no sandbox id;
* both runs exit 0, and the second run's worker is a different Sandbox, not a cached one.

The 1-page and 2-page scans are the preflight; this corpus run is what proves routing, page mapping,
incrementality and a released lease together.

## Teardown verification

Run these after every attempt, including a failed start, a failed readiness wait and a failed run:

```bash
python -m deploy.modal.stop                                  # the lease this shell names, if any
ls "${FINEPDFS_GPU_STATE_DIR:-$HOME/.cache/finepdfs/gpu-workers}"   # any file left is a worker to check
modal container list          # no finepdfs worker should remain; copy any leftover id
modal app list                # finepdfs-gpu-worker may remain as an empty app record (it is free)
```

A provider run passes its own `FINEPDFS_GPU_LEASE_ID`, so its stop command is the one that clears its
file; this plain `stop` only handles the `manual` lease this shell started (or nothing). For a lease file
a crash left behind, name it from its file name:

```bash
lease_file="$HOME/.cache/finepdfs/gpu-workers/0123abcd.json"   # the file the check above listed
FINEPDFS_GPU_LEASE_ID="$(basename "$lease_file" .json)" python -m deploy.modal.stop
```

If a worker remains because stop could not run, the last resort is the operator killing that exact
container id; the lifetime cap is the automatic backstop. Do not delete a lease file by hand while a
worker may exist: it is the only recorded handle on it.

## Assumptions this offline slice did not execute

* **The upstream serving command.** `deploy/gpu/server.py` launches
  `vllm serve PaddlePaddle/PaddleOCR-VL-1.6 --revision c5630aba... --served-model-name PaddleOCR-VL-1.6
  --host 0.0.0.0 --port 8000 --trust-remote-code --max-num-batched-tokens 16384
  --no-enable-prefix-caching --mm-processor-cache-gb 0`. The vLLM recipe requires vLLM >= 0.11.1 and
  this slice pins `vllm/vllm-openai:v0.32.0` by tag and amd64 manifest digest; the pair was not run.
  There is deliberately no command override: if the upstream surface differs, this file and its test
  change, so the served command stays pinned and inspectable.
* **The routes.** `/v1/models` and `/v1/chat/completions` are vLLM's OpenAI surface. The start command
  checks `/v1/models`; the engine calls `/v1/chat/completions` with the page image and prompt `OCR:`.
* **The served-model name.** The pipeline must be configured with
  `FINEPDFS_ENGINE_MODEL=PaddleOCR-VL-1.6`, which the image passes as `--served-model-name`.
* **Whole-page OCR versus the full PaddleOCR-VL pipeline.** Upstream warns that the vLLM service is the
  VLM stage only: a full document parse also needs layout analysis (PP-DocLayout). The shipped engine
  sends a whole rendered page with `OCR:`, so this worker serves that existing contract. If quality is
  unacceptable on the real scans, the fix is a layout-aware server or engine, not a lifecycle change.
* **Authentication.** The model server has no API key; the Modal tunnel is an unguessable HTTPS URL.
  The start payload therefore carries no `api_key`, and no credential is written to stdout or the lease.
  If a later provider needs a bearer token, it comes from the environment and is redacted on errors.
* **Detached sandboxes.** The SDK documentation (1.5.5) says a Sandbox keeps running after its creating
  process exits and can be terminated by id from another process; that is the lifecycle design here,
  checked in the installed source, not against the live service.
* **Image digest.** The digest was read from the Docker Hub tag API on 2026-10-18, not from a local
  pull; a real first build must confirm the platform manifest.
* **The weights cache.** The first real start creates the Modal Volume `finepdfs-hf-cache` and lets
  vLLM download the pinned revision into it. Volume storage and the first download are part of the
  budget gate; later runs reuse the cache.
