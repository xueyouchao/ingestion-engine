# The live upload demo: a visitor's PDF, its OCR Decision, and the deployed worker that reads it

- **Status:** planned. Implements [ADR-0012](../adr/0012-serve-the-upload-demos-ocr-from-a-deployed-modal-app.md).
- **Planning date:** 2026-10-19.
- **Reviewed baseline:** `fa0286e` (the ADR's own branch, `docs/adr-0012-modal-ocr-for-the-live-demo`).
- **Related:** [FinePDFs integration](finepdfs-integration.md), [PDF workflow demo](pdf-workflow-demo.md),
  [glossary](../GLOSSARY.md), [Modal worker runbook](../deploy/modal-worker-runbook.md).

## Problem Statement

Someone evaluating this pipeline can open the static bundle and see what it did to a corpus we picked:
the page image beside the engine's reading, the routing decision, the Chunks. What they cannot do is
bring their own PDF and watch the pipeline decide about it.

The reason is cost and shape, not capability. The probe, `OcrRoutingPolicy` and
`ExtractionStep.route/extract` already produce exactly the decision the visitor wants to see, and the
engines to act on it already exist. But the machine that runs the demo has no GPU, and a live demo is
the wrong load for ADR-0009's unit: one detached Sandbox per *run*, rented by a start command, waited on
for readiness, given back by a stop command. A browser has no lease to hold and no run to end. Every
upload would pay Sandbox creation, image pull, vLLM start and a weights load, and a demo that kept a
worker warm all day would bill for a lane that, measured on this corpus, is idle almost every second:
only 3 pages in 2 of 17 documents needed OCR at all, routing an 84-page PDF took 105 ms, and the native
lane read it in 78 ms.

So the feature is missing for an operator reason: there is nowhere to put the GPU that both an
unpredictable request can reach and an idle afternoon does not pay for.

## Solution

One Modal app is deployed, not leased. It serves the same pinned image and weights as the batch worker,
scales to zero between visitors, and is capped at one container, so the whole demo can hold at most one
GPU. The demo server on the public host accepts a PDF, shows the OCR Decision as a decision -- lane,
reason, the five features behind it and the thresholds they were measured against -- and returns the
page text read by the lane that decision chose.

The visitor never sees a vendor. The endpoint is OpenAI-compatible and is reached through the adapter
that already exists, authenticated with a bearer the app checks, so the same demo runs against a leased
worker by changing one environment variable.

Three Spend Gates bound what a visitor can cost: a page cap on the upload, a rate limit per client, and
a daily budget of OCR pages. When a gate closes the page says which gate it hit and why. A cold worker is
a stage on the page ("warming the OCR worker") followed by the number that was measured, never an error
and never a pretend fast answer. An OCR that fails is a failed Reading, reported verbatim, and the demo
keeps nothing: no uploaded PDF, no extracted text, no page image survives the response.

## User Stories

1. As a visitor, I want to upload my own PDF, so that the demo is about my document and not only the corpus you chose.
2. As a visitor, I want to see which lane my document took (the text layer or OCR), so that I understand what the pipeline decided.
3. As a visitor, I want the reason for that lane in words, so that the decision is auditable rather than a badge.
4. As a visitor, I want the five features the decision was measured on, so that I can see the evidence behind it.
5. As a visitor, I want the thresholds those features were compared against, so that I can tell how close my document was to the other lane.
6. As a visitor, I want the page text the chosen lane actually read, so that the demo proves a reading and not a routing table.
7. As a visitor, I want each page labelled with its original 1-based number, so that a citation still means the page I am looking at.
8. As a visitor, I want the OCR lane's Engine Identity, so that I can see which weights and image produced the text.
9. As a visitor, I want a native PDF read instantly and without a GPU, so that most uploads cost nothing and the demo feels immediate.
10. As a visitor, I want to be told when a worker is warming, so that a slow first OCR does not look like a broken demo.
11. As a visitor, I want the warm-up time reported as a measured number, so that the demo does not claim a latency it never observed.
12. As a visitor, I want an OCR failure reported as a failure with the endpoint's own words, so that I am not shown invented text.
13. As a visitor, I want to be told when the demo is already reading someone else's document, so that I know I am waiting rather than stuck.
14. As a visitor, I want to be told when the daily OCR budget is spent, so that I understand the demo is not broken.
15. As a visitor, I want to be told when my upload exceeds the page cap, so that I can decide to send a smaller document.
16. As a visitor, I want to be told when my upload is too large, or is not a PDF, so that I do not wait for a read that cannot happen.
17. As a visitor, I want to be told when I have read too many documents in a short time, so that the reason for the refusal is clear.
18. As a visitor, I want the page to state that my upload is sent to a rented GPU to be read, so that I know where my bytes go.
19. As a visitor, I want the page to state that nothing is kept after the response, so that I can trust the statement about my document.
20. As a visitor, I want the page to work without a build step or a login, so that I can open the link and use it.
21. As a visitor, I want the limits shown before I upload, so that a refusal is never a surprise.
22. As a visitor, I want the remaining daily budget shown, so that I can see the demo is still open for OCR.
23. As a visitor, I want a mostly-native PDF to be routed by the same policy the batch runs use, so that the demo's decision is the pipeline's decision.
24. As an operator, I want to deploy the OCR worker once and let it scale to zero, so that an idle demo costs storage and not GPU time.
25. As an operator, I want the deployed worker pinned to the same image, weights and detail as the batch worker, so that a Reading from the demo is comparable with a Reading from a run.
26. As an operator, I want a cold start to be a page state and never a new Engine Identity, so that the memo is not invalidated by the address or the lifecycle.
27. As an operator, I want the endpoint to refuse a request without the bearer, so that a public GPU endpoint is not a bill waiting to happen.
28. As an operator, I want the token to live in a Modal Secret and never in the page, the contract or the browser, so that rotating it is one change in one place.
29. As an operator, I want a command that reports and changes whether the worker is held warm, so that I can keep it warm for a session on purpose.
30. As an operator, I want that command to name what a warm worker costs per minute, so that leaving it on is a decision and not an accident.
31. As an operator, I want the warm control to be off by default, so that the demo's resting cost is zero.
32. As an operator, I want the demo server to report which gate it hit and how much budget is left, so that I can size the gates from real use.
33. As an operator, I want the page cap, the rate limit and the daily budget configurable, so that I can change the spend posture without a code change.
34. As an operator, I want one command that prints the address, model, detail and warm state of the deployed worker, so that wiring the demo server is copy and paste rather than derivation.
35. As an operator, I want the demo server to lease its GPU through the existing provider, so that pointing it at a leased worker is one flag.
36. As an operator, I want the demo server to keep no state on disk, so that a demo host holds nothing about anyone's document.
37. As an operator, I want a runbook that says how to check auth, deployment and the cold-start number instead of asserting them, so that the document cannot go stale.
38. As a maintainer, I want the gates to be one pure policy with one order, so that two callers cannot disagree about which gate closed.
39. As a maintainer, I want the demo's engine wiring read from the same environment names the flow reads, so that the demo and a run cannot drift into two engines.
40. As a maintainer, I want the demo to reuse `ExtractionStep` rather than re-decide routing, so that a policy change moves both or neither.
41. As a maintainer, I want the deployed app to import no vendor SDK under `src/`, so that the base install stays vendor-free (ADR-0001, ADR-0007).
42. As a maintainer, I want the demo server's API tested through HTTP with fakes, so that the honest behaviour of stages and refusals is pinned without a GPU.
43. As a maintainer, I want the deploy half tested against a fake SDK, so that the pins, the bearer and the autoscaler calls are pinned offline.
44. As a maintainer, I want a test that the demo keeps nothing, so that the retention promise in the ADR is a check and not a sentence.
45. As a maintainer, I want the glossary to name a Reading, a Spend Gate, a Gate Ledger and the Warm Control, so that tickets and code use one vocabulary.

## Implementation Decisions

### The demo is three modules, one per layer

- **Domain policy** (`domain/demo.py`): the limits, the usage snapshot, the gate decisions, the
  Reading stage names, and the outcome types. Pure; no I/O, no clock, no HTTP.
- **Application use case** (`application/demo_read.py`): `DemoRead.read` turns uploaded bytes and a
  client identity into one Reading. It reuses `ExtractionStep.route`/`extract_with` from
  `application/curate.py` -- routing is *the* policy and the demo must not have its own copy -- and it
  never cleans, language-tags, publishes or deduplicates.
- **Interface** (`interfaces/demo_server.py` + `interfaces/demo_page.py`): the Reading Queue, the HTTP
  server, the job protocol, and the page asset. This is the composition root: it reads `FINEPDFS_*`,
  leases the GPU through `leased_environment`, builds the engine and the endpoint probe, and owns the
  Gate Ledger for the life of the process.

Nothing under `src/` imports Modal, and nothing under `src/` imports `deploy/`.

### A Reading is an outcome, not a bool

`DemoRead.read(content, client, on_stage)` returns exactly one of three outcomes, all carrying the
Routing Decision when one was made:

- **read** -- the lane was chosen, the lane read it, and the outcome carries the per-page text, the
  Engine Identity, the features and thresholds, the elapsed time and (for the OCR lane) how long the
  endpoint took to answer when it was cold.
- **refused** -- the demo declined before or instead of spending: the page cap, the rate limit, the
  daily budget, an unreadable upload, or a full queue. A refusal names its kind and its reason in
  words, and carries no page text.
- **failed** -- a lane was chosen and the read failed. The reason is the underlying error's own words,
  never a summary, and the kind is the exclusion kind `failed` that already exists. No Reading is
  recorded, because the demo records nothing anywhere.

These are three frozen dataclasses with one shared union, not a status string on one object: each has
different fields, and a caller that switches on the type cannot read a page list that is not there.

### The three Spend Gates have one order and one home

The order is ADR-0012's: **page cap, then per-client rate limit, then daily page budget.** The first two
are decided from the Routing Decision's page count and the client identity; the third is decided only
when the chosen lane is OCR, because only OCR pages cost money, and its refusal says the OCR budget is
spent. The page cap and the budget name pages; the rate limit names reads.

The policy is pure: it takes a usage snapshot and returns a decision naming the gate that closed and a
sentence a visitor can read. The counters live behind a **Gate Ledger** port in `domain/ports.py`,
implemented in memory by an adapter under `infrastructure/demo/`. The ledger owns the clock boundary: a
read enters the current window, OCR pages are charged to the current UTC day, and the day rolls over
without a background task. Charging happens when the work is admitted, never on successful completion: a
read that fails still spent the GPU.

### The Reading Queue is the demo server's, and it is bounded

One Reading runs at a time and a bounded number wait behind it. The single slot is what makes the
per-read environment lease safe (a lease mutates `os.environ`, which the flow's own model already
assumes is one run at a time), and it is what ADR-0012 means when it says what serialises uploads is the
demo server's queue and not Modal. A full queue is a refusal with its own kind, not an unbounded wait.

The protocol is therefore a job, not a long request: the upload answers `202` with a job id, and the page
polls that job for its stage. The stages are `queued`, `routing`, `warming`, `reading`, and the terminal
state `done` carries the outcome. This is what makes "warming the OCR worker" a real state rather than
an animation: the server is waiting for the endpoint's `/v1/models` to answer, and it reports how long
that took.

### Warming is measured, not guessed

When the Routing Decision says OCR, the use case asks the endpoint probe whether the endpoint answers.
If it does, the read proceeds with no warming stage. If it does not, the use case announces `warming` and
polls the probe until the warm timeout, then announces `reading`. The measured wait travels back to the
page as a number. The probe is a port: an adapter asks the endpoint's OpenAI-compatible `/v1/models` with
the bearer, and a fake answers in tests. A probe that never answers is a failed Reading whose reason
names the timeout, not a refusal: the gate did not close, the worker did not come up.

### Only page images leave the host, and nothing is kept

The demo server opens the uploaded bytes as a PDF from memory, renders page images from memory for the
OCR lane, and writes no file. It does not persist jobs across a restart. The page states, before the
upload control, that the document is sent to a rented GPU to be read and that nothing is kept after the
response.

### The deployed app is one container, scale to zero, one bearer

The Modal app serves the pinned worker image through a Modal Server (an HTTP server started in
`@modal.enter`) with `max_containers=1`, `min_containers=0`, a scaledown window in the low minutes and a
startup timeout generous enough for the first weights load. It injects the Modal Secret holding the
bearer and passes it to the vLLM launcher, so every `/v1` route refuses a request without it. Modal's own
proxy auth is deliberately not used (ADR-0012). The app is unauthored by `src/`: it lives in
`deploy/modal/`, imports Modal lazily, and shares the pins, the image builder and the model server
launcher with the batch worker.

The app's `@modal.enter` waits for the worker's own `/v1/models` before reporting the container ready, so
a request never lands on a container whose vLLM is still loading, and so the container's startup
timeout is the honest bound on a cold start.

### The Warm Control is a command that names its price

An operator-facing command in `deploy/modal/` reports and changes `min_containers` through Modal's
autoscaler update (no redeploy), prints the serving URL ready to paste (already carrying `/v1`), the
served model and the pinned Engine Identity detail, and names what a held-warm L4 costs per minute from a
pinned rate with its source and observation date. `off` returns the app to its resting zero. It is off by
default because the deploy declares `min_containers=0`.

### One place reads an engine's environment

The flow's runtime and the demo server must read the same `FINEPDFS_*` names into the same `EngineConfig`
and the same `OcrRoutingPolicy`. That mapping is one function in one module, called by both, with a test
that the two consumers agree on a fixed environment. The demo server adds no engine variable of its own;
the only names it owns are the demo's limits.

### The demo server's own limits

Every limit is a default with an environment override, so the spend posture is a setting:

- upload size (default 20 MiB) -- refused in the handler, before the body is fully read;
- page cap per upload (default 25 pages) -- refused from the probe's page count;
- rate limit (default 5 reads per 600 s per client) -- refused per client;
- daily OCR page budget (default 300 pages, UTC day) -- refused when the OCR lane would exceed it;
- Reading Queue depth (default 3 waiting) -- refused when full;
- warm timeout (default 900 s) and poll interval (default 2 s) -- a read that never warms fails.

A client is the left-most `X-Forwarded-For` hop when a proxy set one, else the peer address. This is a
spend brake and not an identity: a public deployment must sit behind a proxy that sets the header, and
the daily budget and `max_containers=1` are the bounds that do not depend on it.

## Testing Decisions

A good test here drives the highest seam that can observe the behaviour and asserts what a visitor or an
operator can see: a stage, an outcome, a refusal kind and its words, a page's text and number, the
identity, a number that was measured, or a pinned contract value. It does not assert private helpers,
intermediate counters that the page never shows, or the order of statements inside a method.

The seams, highest first:

1. **The demo server's HTTP API** (`tests/interfaces/test_demo_server.py`). The whole feature through
   one seam: POST a PDF, poll the job, read the decision and the page texts; watch a refusal per kind; see
   the queue refuse a second upload while one is running. The real `ThreadingHTTPServer` runs on port 0
   with a fake probe, fake engines, a fake endpoint probe and an in-memory ledger. Prior art:
   `tests/interfaces/test_explorer.py`.
2. **The use case** (`tests/application/test_demo_read.py`). `DemoRead` with hand-written fakes, the way
   `tests/application/test_curate_document.py` tests `CurateDocument`: the gate order, a cold endpoint
   announced as a stage and its wait reported, a lane failure verbatim, and the promise that a native
   read never touches the OCR engine.
3. **The gate policy and the ledger** (`tests/domain/test_demo_gates.py`, plus the ledger's own test).
   Boundary values for each gate, the order between them, the day rollover, and that a failed read is
   not refunded. Pure and fast.
4. **The deploy half** (`tests/deploy/test_modal_serve.py`, `tests/deploy/test_modal_warm.py`, and the
   existing contract/image tests extended). A fake Modal module, exactly as `tests/deploy/fake_modal.py`
   already stands in for the SDK: the app declares one container, a zero floor, the pinned image and the
   Secret; the launcher passes the bearer through to vLLM; the warm control computes the price and calls
   the autoscaler update. The existing packaging test that the vendor scripts import Modal lazily grows
   to cover the two new scripts.

The one test that is not through a seam is the retention check: after a successful read nothing new
exists under the working directory or `TMPDIR`. It is cheap, and it is the check behind a sentence in an
accepted ADR.

## Out of Scope

- OCR quality. The demo shows what the model returned; it does not attest that the text is right
  (ADR-0009's open item).
- Any change to the batch worker's lifecycle. ADR-0009's start/stop scripts stay exactly as they are;
  the pins and the image builder are shared, the postures are not.
- Publishing, cleaning, language identification, deduplication, chunking, search, or an index over
  uploaded documents. The demo is a view of Extraction and its Decision, not a curation run.
- Multi-page batch uploads, ZIP uploads, URL fetching, and reading a document twice.
- Durable job state, a database, a background scheduler, or horizontal scaling of the demo server.
- Persistent limits accounting. The ledger is in memory, so a restart forgets the day's spend; that is
  stated in the runbook rather than fixed here.
- RunPod. The provider port already covers it; no second vendor is added.
- A trained router or any change to `OcrRoutingPolicy`'s thresholds.
- Automated cost alerts or scheduled warm windows.

## Further Notes

- The cold-start number does not exist yet. ADR-0012 is explicit that what was measured were whole
  Sandbox runs (450 s for the first one-page run including the first weights download, 167 s for a later
  cold corpus run) and that readiness alone was never timed and the deployed app has never been cold. The
  runbook therefore carries the *command* that measures it and a slot for the number, and the page states
  the number it measured for the visitor in front of it. Neither document may claim a latency nobody
  observed.
- The demo deliberately shows the deployed app's `/v1` base URL as configured, not as embedded: an
  operator pastes it from the Warm Control's output, which is the only place that knows how to turn a
  bare Modal host into an OpenAI base URL.
- A leaked bearer is a bill, not an outage. The runbook names rotation as one Secret update plus one
  environment update on the demo host, and says what the page must never carry.
- The two postures sharing one set of pins is what makes the demo's numbers comparable with a run's. If
  a future change gives the deployed app its own image or model, this spec's promise that a Reading from
  the demo is comparable with a Reading from a run stops being true, and that is a decision, not a
  cleanup.
