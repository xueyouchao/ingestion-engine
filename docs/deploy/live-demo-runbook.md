# Live upload demo runbook

Deploy the demo's OCR worker as one app that scales to zero, wire the demo host to it, run the page, bound
what a visitor can spend, and record the one number nobody has measured yet
([ADR-0012](../adr/0012-serve-the-upload-demos-ocr-from-a-deployed-modal-app.md), spec:
[live upload demo](../spec/live-upload-demo.md)). This file is the **procedure**: the commands, what each
answer means, and where a measured number goes once somebody measures it.

Neither this file nor the page may claim a latency nobody observed. The only times quoted here are
ADR-0009's **whole Sandbox runs**, quoted so that they are not mistaken for a cold start and never in its
place.

> **Status, 2026-10-19.** Written against the merged code on `feature/live-upload-demo`. The deploy half is
> tested offline against a fake Modal SDK (`tests/deploy/test_modal_serve.py`,
> `tests/deploy/test_modal_warm.py`) and the demo server through its own HTTP API
> (`tests/interfaces/test_demo_server.py`). The commands that need no GPU were run in this repository on
> this date -- the pins print, `python -m deploy.gpu.server --print-command`, the demo server and its
> `/api/status`, `modal container list`, `modal secret list` and `modal billing rates`. **Not** run: the
> `modal deploy` itself, the Warm Control against a deployed app, the bearer check against a live
> container, and any measurement of a cold start.

The commands name how to check machine state; they do not claim what the answer will be
(`CODING_STANDARDS.md`).

## Files

| Path | Role |
| --- | --- |
| `deploy/modal/serve.py` | the deployed app: app `finepdfs-demo-ocr`, server class `OcrWorkerServer`, one container, floor 0, the Secret's bearer passed to vLLM |
| `deploy/modal/warm.py` | the Warm Control: `status` / `on` / `off`, the serving URL, the model, the detail, the floor and its price |
| `deploy/gpu/contract.py` | the pins (GPU, container bounds, timeouts), `WORKER_DETAIL`, the L4 rate and `warm_floor_cost()` |
| `deploy/gpu/server.py` | the in-container launcher both postures spawn; `--print-command` prints the pinned command with the bearer masked |
| `deploy/modal/image.py` | the pinned worker image, `WORKER_ENTRYPOINT` and the weights Volume |
| `src/finepdfs/interfaces/demo_server.py` | the demo host: HTTP surface, composition root, `FINEPDFS_DEMO_*` reader |
| `src/finepdfs/interfaces/demo_jobs.py` | the Reading Queue and the job JSON a page polls |
| `src/finepdfs/interfaces/demo_page.py` | the page asset (self-contained HTML) |
| `src/finepdfs/domain/demo.py` | the limits' defaults and `SpendGatePolicy` |
| `src/finepdfs/infrastructure/demo/ledger.py` | `InMemoryGateLedger`, the only Gate Ledger adapter |
| `docs/deploy/modal-worker-runbook.md` | the *other* GPU posture: a Sandbox leased per run. Its budget gate and teardown checks apply to §7 |

## 1. What is deployed, and what it costs at rest

One Modal app, `finepdfs-demo-ocr`, serves one Modal Server class, `OcrWorkerServer`, running the same
pinned image and weights as ADR-0009's batch worker, behind an OpenAI-compatible `/v1` surface. It is one
container at most and zero at rest, and every `/v1` route wants the bearer the Secret injects. Nothing
under `src/` imports Modal: the demo reaches the app through the same `ChatEndpoint` a leased worker uses.

The pins are in `deploy/gpu/contract.py`; read them rather than trusting this table (from the repository
root, in the deploy environment):

```bash
. .venv-deploy/bin/activate          # §2 builds it

python - <<'PY'
from deploy.gpu import contract as c
print(c.DEPLOYED_APP_NAME, c.DEPLOYED_SERVER_NAME, c.DEPLOYED_SECRET_NAME, c.DEPLOYED_SECRET_VARIABLE)
print("gpu", c.DEPLOYED_GPU, "max", c.DEPLOYED_MAX_CONTAINERS, "min", c.DEPLOYED_MIN_CONTAINERS,
      "scaledown", c.DEPLOYED_SCALEDOWN_WINDOW, "startup", c.DEPLOYED_STARTUP_TIMEOUT,
      "exit", c.DEPLOYED_EXIT_GRACE_PERIOD)
print("detail", c.WORKER_DETAIL)
print("warm", c.warm_floor_cost(1).per_minute(), c.warm_floor_cost(1).per_hour())
PY
```

Observed on 2026-10-19 against this commit:

```text
finepdfs-demo-ocr OcrWorkerServer finepdfs-demo-ocr-bearer PADDLEOCR_VL_API_KEY
gpu L4 max 1 min 0 scaledown 180 startup 1200 exit 60
detail paddleocr-vl@1.6:weights-c5630abae1d9:image-6ee133356d49
warm $0.0133 $0.80
```

What each answer is:

| Pin | Meaning |
| --- | --- |
| `DEPLOYED_APP_NAME` / `DEPLOYED_SERVER_NAME` | the app and server name every command below names; `modal deploy` finds `app` in `serve.py` because it is built at module scope |
| `DEPLOYED_SECRET_NAME` / `DEPLOYED_SECRET_VARIABLE` | the Secret that holds the bearer, and the one key it carries (§3) |
| `DEPLOYED_GPU` | the GPU every price in this file is quoted for. Another card has another rate; changing it is an approved-rate decision, not an edit |
| `DEPLOYED_MAX_CONTAINERS` = 1 | the demo can hold at most one GPU, whatever the queue or the visitor does |
| `DEPLOYED_MIN_CONTAINERS` = 0 | the resting floor: the deployed app's **at-rest GPU cost is $0.00 per minute**. `on` in §8 is the only thing that raises it |
| `DEPLOYED_SCALEDOWN_WINDOW` = 180 s | how long a running container survives its last request before it goes away; §9 waits this out |
| `DEPLOYED_STARTUP_TIMEOUT` = 1200 s | the bound on one container's start, which is also the worst-case price of a start that never becomes ready: 1200 s × $0.000222/s = **$0.27** of L4 time, then the container fails |
| `DEPLOYED_EXIT_GRACE_PERIOD` = 60 s | how long vLLM gets to stop in the exit hook before the container goes away |
| `WORKER_DETAIL` | the deployment half of the Engine Identity: model, weights revision and image digest, never an address |

The app is `unauthenticated=True` on purpose: Modal's proxy auth is off, so *the app's own bearer is the
only access control*. An open GPU endpoint is a bill waiting to happen (ADR-0012).

**Cost of the deployed app.** At rest (floor 0) there is no GPU container and the GPU cost is **$0.00 per
minute**; the app record and the `finepdfs-hf-cache` Volume are storage, billed separately, and the Volume
holds weights, not documents. Held warm (floor 1, §8) one L4 container costs **$0.0133 per minute**,
**$0.80 per hour**. Both numbers come from `warm_floor_cost()` and the pinned rate, and the Warm Control
prints the same ones. Nothing else about the demo is billed by Modal: the demo host is an ordinary Python
process.

Check the rate instead of trusting this document, before approving any spending:

```bash
modal billing rates                  # read the "Nvidia L4" line: dollars per hour
```

An L4 hour of $0.80 is $0.000222 per second. If the line says something else, the pins are stale and so is
every number above: `GPU_RATE_PER_SECOND_USD`, `GPU_RATE_SOURCE` and `GPU_RATE_OBSERVED` in
`deploy/gpu/contract.py` are one observation (rate, source URL, date) and all three move together, along
with this table and ADR-0009's budget gate.

**Budget gate -- do not skip.** Deploying costs no GPU time, but every start does. Before the first real
deploy or measurement:

1. the person who owns the Modal workspace has approved a spend cap in writing;
2. the current `modal billing rates` output is recorded and the cap covers the planned starts;
3. `modal profile current` and `modal token info` name the workspace that will be billed (§2);
4. the first thing that runs is §9's single cold start, never a session of uploads;
5. `modal container list` is checked after every session, and `python -m deploy.modal.warm off` is the last
   command of it. The floor is off by default; leaving it on is what costs money between visits.

## 2. Deploying the app

The deploy half is a deploy environment, not the base package: Modal is not a base dependency, and nothing
under `src/` needs it.

```bash
python -m venv .venv-deploy
. .venv-deploy/bin/activate
python -m pip install -r deploy/requirements-modal.txt

modal --version                      # the requirements pin 1.5.5; anything else is a changed environment
modal profile current                # the profile (and so the workspace) that will be billed
modal token info                     # authenticated? names the token, workspace and user that get billed
python -c "import modal; print(modal.__version__)"
```

From the repository root, with that environment active:

```bash
modal deploy deploy/modal/serve.py
```

`serve.py` builds `app` at module scope precisely so `modal deploy` can find it. The deploy registers the
app and its server; it starts no container, so a deploy on its own costs no GPU time. What to check
afterwards, and what each answer means:

```bash
modal app list                       # --json for machine-readable output
modal container list                 # --json for machine-readable output
modal app logs finepdfs-demo-ocr     # the @modal.enter hook's own words; -f to follow, --tail N for more
```

* `modal app list` shows `finepdfs-demo-ocr` with state `deployed`: the app exists in this workspace and
  environment, and the server URL the Warm Control reads is available. A `Tasks` count of 0 means no
  container is running -- that is the resting posture, not a fault.
* No row for `finepdfs-demo-ocr` in `modal app list`: not deployed here. Either the deploy failed, or you
  are looking at another profile or environment (`modal profile current`, `-e/--env`).
* `modal container list` prints an empty "Active Containers" table (and `[]` with `--json`) when nothing is
  running; a row whose App Name is `finepdfs-demo-ocr` is a container that exists right now and bills by
  the second until the scaledown window passes. This is the command §8 and §9 use. Use `--json` or a wide
  terminal: the App Name column is truncated in a narrow one.
* `modal app logs finepdfs-demo-ocr` is where the start hook's failure lands. `@modal.enter` spawns the
  shared launcher (`deploy/gpu/server.py`), waits for the worker's own `/v1/models` with the bearer, and
  only then reports the container ready; a worker that never answers is stopped there and the container
  fails its start. A container that is listed and then disappears with an error in the log is that path,
  not a request that failed.

The command the container will run, printed without starting anything -- and with the bearer masked as
`***`, so the output is safe to paste:

```bash
PADDLEOCR_VL_API_KEY=placeholder-not-a-real-token \
PADDLEOCR_VL_MODEL=PaddlePaddle/PaddleOCR-VL-1.6 \
PADDLEOCR_VL_REVISION=c5630abae1d940eafe0697512a0325494b02ab42 \
PADDLEOCR_VL_SERVED_MODEL_NAME=PaddleOCR-VL-1.6 \
PADDLEOCR_VL_PORT=8000 \
python -m deploy.gpu.server --print-command
```

The image sets those four variables; `PADDLEOCR_VL_API_KEY` is not the image's, it is the Secret's. Any
value in that slot prints as `***`.

## 3. Creating the bearer Secret

The Secret is created once per workspace and has one key:

```bash
# a token nobody can guess, printed once, never written to the repository
python -c 'import secrets; print(secrets.token_urlsafe(32))'

modal secret create finepdfs-demo-ocr-bearer PADDLEOCR_VL_API_KEY=<the token>
```

Passing `PADDLEOCR_VL_API_KEY=<the token>` puts the token in your shell history; if that matters, put the
line `PADDLEOCR_VL_API_KEY=<the token>` in a file outside the repository (a `.env`-shaped file is not
gitignored here) and use `modal secret create finepdfs-demo-ocr-bearer --from-dotenv <that file>`.
`modal secret create ... --force` overwrites an existing Secret; that is §10.

Check it, and what each answer means:

```bash
modal secret list                    # --json for machine-readable output
```

* a row named `finepdfs-demo-ocr-bearer`: the key exists in this workspace and environment. The creation
  time is how you tell a rotation happened.
* no row: the app cannot serve. `serve.py` asks for the Secret with `required_keys=[PADDLEOCR_VL_API_KEY]`,
  so an app deployed without it fails to resolve the Secret -- at deploy, or at the latest when its first
  container starts -- and a container whose environment lacked the key refuses to start its worker
  (`ServeConfigurationError` names the variable and the Secret). Neither path serves without a bearer.

The app passes the key to vLLM as `--api-key`, so every `/v1` route -- `/v1/models` included -- answers
only to `Authorization: Bearer <the token>`. The Adapter sends exactly that header
(`infrastructure/engines/vlm_chat.py`). The token never reaches the page, `/api/status`, the contract JSON,
a log line or the repository.

## 4. Wiring the demo host from the Warm Control's output, and checking the endpoint answers

The Warm Control is the only thing that knows how to turn the bare Modal host into an OpenAI base URL, so
the demo host is wired by pasting its output. From the repository root in the deploy environment:

```bash
python -m deploy.modal.warm status
```

`status` prints, verbatim, six lines; each one's meaning and what it becomes on the demo host:

| Line | What it is | Demo host variable |
| --- | --- | --- |
| `base_url: https://.../v1` | the serving URL, **already ending at `/v1`** (`ChatEndpoint` appends only `/chat/completions`) | `FINEPDFS_ENGINE_BASE_URL` |
| `model: PaddleOCR-VL-1.6` | the served model name, pinned by the image | `FINEPDFS_ENGINE_MODEL` |
| `detail: paddleocr-vl@1.6:weights-...:image-...` | the Engine Identity's deployment half, **without** a page-DPI suffix | `FINEPDFS_ENGINE_DETAIL` |
| `floor: 0` | the current minimum container count | -- |
| `cost_per_minute: $0.0000` | what that floor costs per minute at the pinned rate | -- |
| `rate: $0.000222/s per L4 container (source: ..., observed: ...)` | the rate the price came from | -- |

The `detail:` line carries no `@dpi200`: the engine registry appends the page DPI itself for
`paddleocr-vl` (`DPI_CARRYING_ENGINES` in `infrastructure/engines/registry.py`), so pasting a
`@dpi200`-suffixed detail would produce `@dpi200@dpi200` and put a detail nothing else uses into the memo
key. The bearer is the one thing not in that output; it is the token from §3.

```bash
export FINEPDFS_ENGINE_BASE_URL="<the base_url: line>"
export FINEPDFS_ENGINE_MODEL="<the model: line>"
export FINEPDFS_ENGINE_DETAIL="<the detail: line, with no @dpi200>"
export FINEPDFS_ENGINE_API_KEY="<the bearer you hold>"
export FINEPDFS_ENGINE=paddleocr-vl      # the demo's own default; set it only to differ from it
```

Those four names are the same ones a run reads, and the demo has no engine variable of its own.
`FINEPDFS_ENGINE` selects the engine the demo's OCR lane asks for; the demo defaults to `paddleocr-vl`
where a run defaults to the native text layer.

Check that the demo host answers, without spending anything:

```bash
curl -fsS http://127.0.0.1:8766/api/status      # the demo server must be running (§5)
```

Observed on 2026-10-19 with the demo's defaults and no endpoint configured:

```json
{"limits": {"page_cap": 25, "rate_limit_reads": 5, "rate_limit_window_seconds": 600,
 "daily_ocr_page_budget": 300, "upload_bytes": 20971520, "queue_depth": 3,
 "warm_timeout_seconds": 900, "poll_interval_seconds": 2},
 "ocr_pages_remaining": 300, "queue": {"active": 0, "waiting": 0}, "ocr_endpoint_configured": false}
```

What each answer means:

* `ocr_endpoint_configured: true` -- the demo found a base URL (or a worker start command) in its
  environment. It did **not** contact the worker to find out, so this is not proof the bearer matches or
  the app is up; it is proof the host has somewhere to send the OCR lane.
* `ocr_endpoint_configured: false` -- no OCR endpoint. Native reads still work; an upload that routes to
  OCR fails with kind `failed` on lane `ocr`. This is the answer to check before a session.
* `limits` and `ocr_pages_remaining` -- the spend posture actually in force (§6), and the budget left
  today. Both are the ledger's current numbers, which restart at zero with the process (§12).
* `queue` -- one Reading active at most and how many wait behind it.
* The bearer and the address never appear in this answer, by design.

There is deliberately no free check of the worker itself. `GET /v1/models` is the readiness surface, and
with the floor at 0 *asking it starts a container*: that request is the first charge of the session and it
would ruin a cold-start measurement. If you need to know the bearer matches before a session, do it
knowingly:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $FINEPDFS_ENGINE_API_KEY" \
  "$FINEPDFS_ENGINE_BASE_URL/models"
```

* `200` -- the endpoint answered with your bearer, the model is served, and a container is now running and
  billing until §8's `off` or the scaledown window ends it.
* `401` (or `403`) -- the token does not match the Secret, or no `Authorization` header arrived. The
  request still woke a container, so this answer costs the same as a `200`.
* a timeout or a connection error after roughly `DEPLOYED_STARTUP_TIMEOUT` -- the container started but its
  worker never answered `/v1/models`; `modal app logs finepdfs-demo-ocr` and `modal container list` say
  what happened (§2). A worker that never answers is stopped by the app and the container fails its start.

## 5. Running the demo server

The demo host is an ordinary Python process. On the host that will serve the page, with the four engine
variables exported as in §4:

```bash
head -1 .venv/bin/finepdfs-live-demo    # the interpreter the console script was built against

.venv/bin/finepdfs-live-demo            # binds 127.0.0.1:8766
# or, without an installed console script:
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo_server
```

If the first line names an interpreter that is not on that machine, use the module form. It prints the one
line an operator needs:

```text
serving the live upload demo at http://127.0.0.1:8766
```

Defaults are host `127.0.0.1` and port `8766` (the explorer owns 8765). `--host` and `--port` move it, and
`--host 0.0.0.0` is the deliberate act of putting it behind a proxy -- the rate limit reads the left-most
`X-Forwarded-For` hop when a proxy set one, else the peer address, so a public deployment belongs behind a
proxy that sets it.

The HTTP surface, and what each answer means:

| Request | Answer |
| --- | --- |
| `GET /` | the page: the statements about where the bytes go and what is kept, the limits and the remaining budget before the upload control, and the decision, the Engine Identity and the page text once a Reading arrives |
| `GET /api/status` | the §4 status object: limits, remaining OCR budget, queue depth, whether an OCR endpoint is configured -- never the bearer or the address |
| `POST /api/read` | the PDF as the body, `Content-Type: application/pdf`; `202 {"job": "<id>"}`; `413` over the byte cap; `400` for an empty body, the wrong media type or no `Content-Length`; `503` when the queue is full |
| `GET /api/read/<id>` | the job JSON: `stage`, `outcome`, `elapsed_ms`, `warmed_ms` (`null` unless the OCR lane waited for a cold worker), `refusal`, `failure`, `reading` (lane, reason, the five features, the thresholds, the Engine Identity, and each page with its original 1-based number); `404` for an unknown id |

`POST` answers immediately; the page polls the job. The stages are `queued`, `routing`, `warming`,
`reading`, and `done` carries the outcome. A visitor sees a slow first OCR as a measured warm-up
(`Warm-up: N.N s measured waiting for the cold OCR worker.`), never as an error and never as a fake fast
answer; a failed OCR is a failed Reading in the endpoint's own words.

The page is a view: it never curates, never publishes and never writes (§11).

## 6. The three Spend Gates, and how to change them

`SpendGatePolicy` (`domain/demo.py`) applies the three gates in this order and stops at the first that
closes. The counters it reads come from the Gate Ledger; the defaults live in the same module, so the
defaults below and the server's reading cannot disagree. Setting a limit means setting its variable before
the server starts, or passing its flag for one run:

| Variable | Flag | Default | What it bounds | Refusal |
| --- | --- | --- | --- | --- |
| `FINEPDFS_DEMO_PAGE_CAP` | `--page-cap` | 25 pages | pages in one upload | kind `page-cap` |
| `FINEPDFS_DEMO_RATE_LIMIT_READS` per `FINEPDFS_DEMO_RATE_LIMIT_WINDOW_SECONDS` | `--rate-limit-reads`, `--rate-limit-window-seconds` | 5 reads per 600 s, per client | one visitor's reads in a sliding window | kind `rate-limit` |
| `FINEPDFS_DEMO_DAILY_OCR_PAGES` | `--daily-ocr-pages` | 300 OCR pages per UTC day | the demo's whole OCR spend for the day, across all visitors | kind `daily-budget` |

Three more settings are the demo's but are not spend: `FINEPDFS_DEMO_UPLOAD_BYTES`
(`--upload-bytes`, default `20971520`, 20 MiB -- refused as an HTTP `413` with kind `upload` before the
body is read), `FINEPDFS_DEMO_QUEUE_DEPTH` (`--queue-depth`, default 3 Readings waiting -- refused as an
HTTP `503` with kind `busy`) and `FINEPDFS_DEMO_WARM_TIMEOUT_SECONDS` / `FINEPDFS_DEMO_POLL_INTERVAL_SECONDS`
(`--warm-timeout`, `--poll-interval`, defaults 900 s and 2 s -- a read that never warms fails rather than
waiting for ever).

```bash
export FINEPDFS_DEMO_DAILY_OCR_PAGES=50          # the variable is the configuration
export FINEPDFS_DEMO_RATE_LIMIT_READS=2
.venv/bin/finepdfs-live-demo                      # reads both

.venv/bin/finepdfs-live-demo --daily-ocr-pages 50  # a flag overrides that one variable for this run,
                                                   # and replaces no other
```

What the gates promise:

* a page-cap or rate-limit refusal charges nothing -- no page against the day, no read against the window;
* the daily budget is asked only when the chosen lane is OCR, so a native read is admitted even after the
  OCR budget is spent;
* pages are charged when the work is admitted, and a failed or abandoned Reading is not refunded the same
  GPU money it already spent (§12);
* a limit that is not the kind of number it takes (`FINEPDFS_DEMO_PAGE_CAP=ten`, a negative value) is an
  error naming the variable, and the server exits 2 instead of starting with a posture nobody asked for.

Change the posture and check it took effect with `curl -fsS http://127.0.0.1:8766/api/status` (§4): the
`limits` object is what the running process is enforcing, `ocr_pages_remaining` is what is left today.
An exported-but-empty variable means the default, exactly as it does for the engine's own reader.

## 7. Pointing the demo at a leased worker instead of the deployed app

One variable changes the GPU posture, and nothing in the demo's own configuration moves: the demo leases
through the existing provider for the length of each Reading and reads the endpoint the lease publishes.
The commands are the ones the
[Modal worker runbook](modal-worker-runbook.md#start-and-stop) already documents, run from the repository
root:

```bash
export FINEPDFS_GPU_PROVIDER=command
export FINEPDFS_GPU_START_COMMAND="$PWD/.venv-deploy/bin/python -m deploy.modal.start"
export FINEPDFS_GPU_STOP_COMMAND="$PWD/.venv-deploy/bin/python -m deploy.modal.stop"
export FINEPDFS_GPU_STATE_DIR="$PWD/.gpu-worker-leases"   # one lease file per Reading; gitignored
```

Leave the four `FINEPDFS_ENGINE_*` variables from §4 unset in this posture: the provider publishes the
leased address itself, for the length of the Reading, and restores the environment afterwards. A pinned
`FINEPDFS_ENGINE_DETAIL` is kept -- both postures carry the same pins, so the same detail names both, and a
Reading taken one way stays comparable with a Reading taken the other way.

What changes when you do:

* every Reading whose lane is OCR rents and releases a whole Sandbox: the start, the readiness wait and the
  stop are part of that upload's wait, so a cold upload here is bounded by ADR-0009's numbers, not by a
  container's scaledown;
* ADR-0009's budget gate and teardown checks apply, in full, to this posture: `modal container list` after
  every session, and `ls "$FINEPDFS_GPU_STATE_DIR"/*.json` showing no lease left behind (the worker
  runbook's "Teardown verification"). A leaked lease bills by the hour;
* `python -m deploy.modal.warm status|on|off` (§8) is about the deployed app and has nothing to say about
  these Sandboxes;
* an upload the demo cannot even lease for (a start command that exits nonzero, a provider that cannot be
  configured) is refused with kind `unavailable`: the gates did not close and no lane failed, the demo could
  not reach its worker. A lease that publishes an endpoint which then never answers is a different outcome
  -- a failed Reading (kind `failed` on lane `ocr`), after its pages were charged.

## 8. Holding the worker warm, and what it costs per minute

The Warm Control reports and moves the deployed app's `min_containers` through Modal's autoscaler update;
no redeploy is involved. From the repository root in the deploy environment:

```bash
python -m deploy.modal.warm status   # the six lines of §4
python -m deploy.modal.warm on       # floor 1: hold one container warm, and print its price per hour
python -m deploy.modal.warm off      # floor 0: back to the resting zero
```

`on` prints the same `floor`, `cost_per_minute` and `rate` lines as `status`, plus `cost_per_hour`. What
each answer means:

* `floor: 1` / `floor: 0` -- the minimum container count Modal now targets. It is a target: whether a
  container actually exists is `modal container list` (§2).
* `cost_per_minute: $0.0000` at floor 0 -- the deployed app at rest: no GPU container, no GPU cost.
* `cost_per_minute: $0.0133` and `cost_per_hour: $0.80` at floor 1 -- one L4 container held warm. This is
  the deployed app's warm cost, and leaving it on for a day is 24 × $0.80 = $19.20 of L4 time.
* `error: Lookup failed for Function 'OcrWorkerServer' from the 'finepdfs-demo-ocr' app: App
  'finepdfs-demo-ocr' not found in environment '...'` (exit 1, the answer SDK 1.5.5 gives for an app that is
  not deployed where you are looking), or `error: ... has no serving URL; is it deployed?` (also exit 1):
  deploy the app (§2) first, and check `modal profile current` / `-e` if you believe it is deployed.
* a wrong argument prints the usage line and exits 2, changing nothing.

`status` reads the current floor through an autoscaler update that requests no change, so it changes
nothing and costs nothing. Holding the worker warm is a decision for a session, not a fix: with the floor
at 1 the demo's resting cost is no longer zero, which is the whole point of ADR-0012's scale-to-zero.

Check what is actually held, and put it back:

```bash
modal container list                 # a row for finepdfs-demo-ocr = a container is running and billing
python -m deploy.modal.warm off      # the last command of every session
modal container list                 # right after `off` a container may still be inside its window (§9)
```

## 9. The cold-start slot: measuring the first OCR after idle

**Nothing has measured a cold start of the deployed app.** ADR-0012 says so, the spec says so, and the page
states the warm-up it measured for the visitor in front of it rather than a latency from this file. Until
somebody runs the procedure below, the number is:

> **MEASURED-AT-COLD-START: _unmeasured_**
>
> Recorded: no run. Procedure: this section. When it is filled, it becomes the wait the demo measured
> between deciding that the lane is OCR and the endpoint first answering `/v1/models`, which the job
> reports as `warmed_ms` in milliseconds -- recorded with the date, the SDK version, the Modal environment,
> whether the Volume already held the weights, the page count and `elapsed_ms` beside it.

What ADR-0009 did measure, and what it did **not**: four whole Sandbox runs on 2026-10-18, each one start,
readiness, the read and a stop -- 450 s for the first one-page run (including the first weights download
into `finepdfs-hf-cache`), 126.5 s for the one-page pipeline run, 167 s for the later cold corpus run and
153 s for the warm one. Readiness on its own was never timed, and none of those numbers is a cold start of
the deployed app: they are whole-run walls that include a Sandbox being created, a read and a teardown.
**They cannot be quoted in place of the number above.**

The procedure, in order. Every step is a command and an answer to judge, not an instruction to believe.

1. **Budget and one warm run first.** Satisfy the budget gate (§1). Then make one OCR-routed read with the
   floor at 1 (§8), so the Volume holds the weights and the endpoint is proven end to end. A start measured
   before any container has loaded the weights is a different measurement -- it includes the first
   download -- so if you must measure that one, say so in the record.

2. **Drop the floor to zero.**

   ```bash
   python -m deploy.modal.warm off      # prints floor: 0
   ```

   `floor: 0` is the target. A container that is already running stays until its scaledown window passes.

3. **Confirm a container is still there or already gone.**

   ```bash
   modal container list --json          # [] means none; a row names the app that holds it
   ```

   An empty list (`[]`) means no container exists in this workspace and environment at all, so the next
   request pays a full start. A row whose App Name is `finepdfs-demo-ocr` means one of the app's containers
   is still up: the next request would be warm and must not be recorded as a cold start. A row for some
   other app says nothing about this one -- the list is per environment, not per app; `--app-id
   <the id from modal app list>` narrows it.

4. **Wait out the scaledown window, then confirm again.** `DEPLOYED_SCALEDOWN_WINDOW` is 180 s, so wait a
   little longer than that:

   ```bash
   sleep 200
   modal container list --json
   ```

   `[]` here is the precondition for the measurement: no container existed before the request, so the wait
   the read reports is a container start. A row for `finepdfs-demo-ocr` that is still there means it has not
   scaled down (a request may have reached it, or the window has not passed); wait and check again. If it
   never goes, do not record a number -- measure nothing rather than measure a warm worker.

5. **Send one PDF that routes to OCR.** A genuine image-only scan, like ADR-0009's one-page scan: a PDF with
   a text layer never needs OCR, never shows `warming` and reports `warmed_ms: null`. The rate limit counts
   this read, so send it once. The upload answers `202` and a job id; the read itself takes as long as the
   start does, so poll the job until its stage is `done`:

   ```bash
   SCAN=/path/to/the-one-page-scan.pdf

   JOB="$(curl -fsS -H 'Content-Type: application/pdf' \
     -H "X-Upload-Name: $(basename "$SCAN")" \
     --data-binary "@$SCAN" http://127.0.0.1:8766/api/read \
     | python -c 'import json,sys; print(json.load(sys.stdin)["job"])')"

   until [ "$(curl -fsS "http://127.0.0.1:8766/api/read/$JOB" \
             | python -c 'import json,sys; print(json.load(sys.stdin)["stage"])')" = done ]; do
     sleep 5
   done

   curl -fsS "http://127.0.0.1:8766/api/read/$JOB" | python -m json.tool
   ```

6. **Read the number.** Under the page's stage timeline the measured wait is printed as
   `Warm-up: N.N s measured waiting for the cold OCR worker.`; the same wait is the job's `warmed_ms`, in
   milliseconds. The job also carries `elapsed_ms` (the whole read, warming included) and, in `reading`, the
   `engine` identity and the pages, so a failed or empty read is visible in the same answer and is not a
   start time. Two answers are not a measurement: `null` for `warmed_ms` (the worker was already answering,
   so it was not cold) and a `failure` (the worker never answered -- a failed Reading whose reason names the
   timeout, not a start duration).

7. **Record it** in the slot above, and put the floor back if the session is over (`python -m
   deploy.modal.warm off`, then `modal container list`). One measurement is one start on one day in one
   environment; note the SDK version and whether the Volume was warm, so a later reader can tell whether
   two numbers are comparable.

## 10. Rotating the bearer

A leaked bearer is a bill, not an outage: the app's proxy is unauthenticated, so the bearer is the only
thing between the internet and a GPU container. Rotate it as one Secret change plus one demo-host change,
in the order below, when no container is running (so no container is left answering to the old token):

```bash
modal container list                  # [] is the clean moment to rotate; a row means one is still up (§9)

python -c 'import secrets; print(secrets.token_urlsafe(32))'   # the new token, printed once

modal secret create finepdfs-demo-ocr-bearer PADDLEOCR_VL_API_KEY=<the new token> --force

modal secret list                     # the finepdfs-demo-ocr-bearer row's creation time is now the rotation

modal deploy deploy/modal/serve.py    # so the next container starts vLLM with the new key
modal app logs finepdfs-demo-ocr      # the @modal.enter hook's own report for that deploy
```

Then, on the demo host, replace `FINEPDFS_ENGINE_API_KEY` with the same new token and restart the demo
server (§5). Order matters because both sides must agree:

* a container that is already running keeps the token vLLM was started with, which is why an empty
  `modal container list` is the clean moment; if one is still up and you must rotate anyway, `modal app stop
  finepdfs-demo-ocr -y` terminates running containers and `modal app list` tells you it went to zero, then
  redeploy;
* between the Secret change and the demo host's restart, the demo holds a token the app no longer serves:
  the OCR lane fails with `401` in the endpoint's own words, and a page that was charged for that read is
  not refunded (§12);
* the only proof that the pair matches is one OCR read -- `curl -fsS "http://127.0.0.1:8766/api/read/<id>"`
  on a job that routes to OCR answering a `reading`, not a `failure`. It is a paid check, so do it once, on
  the smallest image-only page you have.

Rotate immediately if the token reached a shell history you do not control, a ticket, a PR, a log or a
screenshot. The new token goes to the same two places and nowhere else: the Secret, and the demo host's
`FINEPDFS_ENGINE_API_KEY`. The page, `/api/status`, the contract JSON and this repository must never carry
it.

## 11. What the demo does not do

The demo is a view of Extraction and its OCR Decision, not a curation run. What it does not do, so that
nobody reads more into its page than is there:

* no curation run: the routing decision is `OcrRoutingPolicy`'s and nothing follows it -- no Cleaning, no
  Page Language, no language policy, no Publish Policy, no Exclusion record on disk;
* no extraction record, no published record, no corpus, no file written: not one byte under the working
  directory or `TMPDIR` -- the upload is opened from memory and the page images are rendered from memory;
* no chunk export, no search index, no query answer: a Reading has pages, not Chunks;
* no Deduplication: one upload is one Document, and it is never compared with anything;
* no URL verification: there is no source URL, and nothing about the visitor or their document is resolved;
* no OCR quality claim: the page shows what the model returned, and ADR-0009's OCR-quality item is still
  open;
* nothing retained: only page images leave the host, for the length of the Reading; the uploaded bytes are
  dropped when the Reading finishes, the job objects live only in memory (bounded), and the Modal Volume
  holds weights, not documents. The page states all of this before the upload control;
* no durable state: a restart loses every job, every counter and every queued Reading.

## 12. Known limits of the counters

The Gate Ledger is in memory, and that has consequences a reader of `/api/status` should know:

* **In memory, per process.** Restarting the demo server forgives every counter: the day's OCR spend, every
  visitor's window. It is a spend brake for one running process, not an accounting record, and two
  processes behind one address would keep two ledgers and show two different remaining budgets. The demo is
  one process by design (one Reading at a time, one lease at a time).
* **A visitor is an address.** The rate limit counts the left-most `X-Forwarded-For` hop when a proxy set
  one, else the peer address. A shared address (an office, a phone network, a proxy) is one visitor for the
  rate limit, and a changed address is a new visitor. The bounds that do not depend on it are the daily
  budget and `max_containers=1`.
* **The day is UTC and rolls over lazily.** OCR pages are charged to the UTC day they were admitted on; the
  next answer the ledger gives after midnight starts the new day at zero. There is no scheduler and no
  background task.
* **Charged on admission, never refunded.** The daily budget counts admitted OCR pages. A Reading that then
  fails, times out while warming or is abandoned by its visitor still spent the GPU, so its pages stay
  charged. A refusal charges nothing.
* **The queue refuses rather than waits.** One Reading runs at a time and `FINEPDFS_DEMO_QUEUE_DEPTH` wait
  behind it; the next upload gets an HTTP `503` with kind `busy`. There is no unbounded wait, and an upload
  dropped by a visitor mid-queue still occupies its place until it runs.
* **The warm timeout is a failure, not a refusal.** A worker that never answers within
  `FINEPDFS_DEMO_WARM_TIMEOUT_SECONDS` makes the Reading fail with kind `failed` on lane `ocr`, in the
  error's own words; the gate did not close, the worker did not come up.
* **`/api/status` is this process's view.** Its `limits` are the ones in force since the process started
  and its `ocr_pages_remaining` is this process's ledger; changing a variable takes a restart, and a
  restart resets the number.
