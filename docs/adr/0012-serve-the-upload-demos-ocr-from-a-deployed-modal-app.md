# 12. Serve the live demo's OCR from a deployed Modal app that scales to zero

Date: 2026-10-19

## Status

Accepted. Scope: the live upload demo. Batch runs keep ADR-0009's per-run Sandbox.

## Context

ADR-0011 makes the pinned Corpus a static page. The live demo is the other shape: a visitor uploads a
PDF, the routing decision is shown as a decision (lane, reason, the five features behind it), and the
page comes back read by the lane that decision chose. The first two steps are already a use case -- the
probe, `OcrRoutingPolicy`, and `CurationService.route/extract` in `application/curate.py` -- so the only
new question is where the GPU lives when a request arrives that nobody scheduled.

ADR-0009's unit is the wrong one for that. It rents one detached Sandbox per *run*: a start command
creates it, the command provider waits for `/v1/models`, the endpoint is printed to stdout, and a stop
command terminates it. Every upload would pay Sandbox creation, image pull, vLLM start and weights load,
and a browser has no lease to hold. The demo also must not hold a GPU warm all day: measured on this
corpus, routing an 84-page PDF takes 105 ms and the native lane reads it in 78 ms, while only 3 pages in
2 of 17 documents needed OCR at all. A worker kept warm 24/7 would bill for a lane that is idle almost
every second.

The pieces that make a deployed app cheap to adopt already exist. ADR-0007 keeps the address out of the
memo key and requires a stable detail; ADR-0009 pins the image and the weights and proved the worker on
Modal. `deploy/gpu/contract.py` holds the pins -- `PaddlePaddle/PaddleOCR-VL-1.6` at
`c5630abae1d9…`, vLLM's OpenAI image at `6ee133356d49…`, port 8000, weights in the `finepdfs-hf-cache`
Volume -- and `deploy/modal/image.py` builds that image. Nothing about *what* runs needs deciding. Only
*how it is made reachable for an unpredictable request* does.

## Decision

**Deploy, do not lease, for the demo.** One Modal app serves the same pinned image as a web endpoint.
`max_containers=1` bounds the demo to a single GPU, and `scaledown_window` in the low minutes lets that
container go away between visitors, so an idle demo holds no GPU at all. The vLLM server inside the
container answers concurrent requests itself; what serialises uploads is the demo server's own queue, not
Modal. ADR-0009's start/stop scripts stay exactly as they are for batch runs, and both postures read
their pins from the same module, so they cannot drift into two different engines.

**The endpoint is OpenAI-compatible and is reached through the existing adapter.** The demo passes a
`base-url`, a model name and a key, exactly as a leased worker does. No Modal SDK is imported under
`src/`: a vendor is a transport detail (ADR-0001, ADR-0007), and the demo must be able to run against a
leased worker with one flag.

**It is authenticated with a bearer the app checks.** The adapter already sends
`Authorization: Bearer <api key>` (`infrastructure/engines/vlm_chat.py`), so the app compares that token
against a Modal `Secret` and refuses anything else. Modal's own proxy auth is the other option and is not
taken: it authenticates with `Modal-Key`/`Modal-Secret` headers the adapter does not send, so it would
mean teaching the pipeline about a vendor to gain nothing. The token lives in the demo server's
environment and never reaches the page, the contract or the browser: an unauthenticated GPU endpoint is a
bill waiting to happen.

**The Engine Identity is the pins, not the address.** The deployed app reports the same
`paddleocr-vl@1.6:weights-c5630abae1d9:image-6ee133356d49@dpi200` detail the batch worker does, so a cold
start cannot look like a new engine and invalidate the memo (ADR-0005, ADR-0007), and a weights or image
change is the only thing that does.

**Cold start is a state of the page, not an error.** The first OCR after idle pays container start plus
loading weights from the Volume. The demo shows that as a stage ("warming the OCR worker") and states the
number it measured instead of pretending the request was fast. A session may hold the worker warm by
setting `min_containers=1`, which `Function.autoscaler.update()` changes without a redeploy; the control
names what that costs per minute, and it is off by default
([Modal's autoscaler settings](https://modal.com/docs/guide/scale)).

**Spend has three gates, in this order.** A per-request page cap, then a per-IP rate limit, then a daily
page budget. When a gate closes the demo answers with the gate it hit -- "the OCR budget for today is
spent" -- and never queues indefinitely or invents a reading.

**A failed OCR is a failed reading.** If the endpoint is down, times out or answers an error, the uploaded
document's OCR lane carries that outcome verbatim and no reading is recorded. The exclusion kind `failed`
already exists; the demo reuses it rather than papering over it.

**Only page images leave the host, for the length of the request.** The demo server keeps nothing: no
uploaded PDF, no extracted text, no page image after the response. The Modal Volume holds weights, not
documents. The pinned marine-corpus pages are already public, and the page says that an upload is sent to
a rented GPU to be read.

## Consequences

+ An idle demo costs storage, not GPU time: scale-to-zero is the default posture, and the only paid window
  is the one somebody's request opened.
+ Demo and batch share one adapter, one set of pins and one identity convention, so a reading taken by
  the demo is comparable with a reading taken by a run.
+ The blast radius is bounded by construction: one container, one request, one budget.
- The first OCR after idle is slow and the demo will feel it. A Sandbox start here measured 450 s for the
  very first start including the weights download, and 167 s for a later cold corpus run; the deployed
  app's cold start with a warm Volume is **not yet measured**, and until it is, the demo cannot claim a
  latency. That number belongs in the demo's evidence next to ADR-0009's.
- Two GPU postures now exist -- a Sandbox per run and a deployed app -- with the same image and different
  lifecycles. The pins are shared; the runbooks are not.
- A public GPU endpoint is an attack surface. It must stay authenticated, be rate-limited, and have its
  token rotated by an operator; the failure mode of getting this wrong is a bill, not an outage.
- OCR quality remains unmeasured (ADR-0009's open item). The demo shows what the model returned; it does
  not attest that the text is right.
- This ADR does not decide the demo server's own limits (upload size, page count, timeouts) beyond naming
  them as gates; those belong to the demo's own record when it is built.
