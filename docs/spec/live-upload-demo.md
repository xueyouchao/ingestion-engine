# The live upload demo: a visitor's PDF, its OCR Decision, and the deployed worker that reads it

- **Status:** implemented on `feature/live-upload-demo` -- the demo server, the deployed app and the Warm
  Control, tested offline. Not deployed and not measured; the operator's record is
  [the live demo runbook](../deploy/live-demo-runbook.md). Implements
  [ADR-0012](../adr/0012-serve-the-upload-demos-ocr-from-a-deployed-modal-app.md). The page's three panes
  are [ADR-0014](../adr/0014-stream-the-raw-read-beside-the-rendered-page.md)'s (accepted 2026-10-22), and
  they are recorded here as the page contract.
- **Planning date:** 2026-10-19.
- **Reviewed baseline:** `fa0286e` (the ADR's own branch, `docs/adr-0012-modal-ocr-for-the-live-demo`).
- **Related:** [FinePDFs integration](finepdfs-integration.md), [PDF workflow demo](pdf-workflow-demo.md),
  [glossary](../GLOSSARY.md), [ADR-0014](../adr/0014-stream-the-raw-read-beside-the-rendered-page.md),
  [live demo runbook](../deploy/live-demo-runbook.md),
  [Modal worker runbook](../deploy/modal-worker-runbook.md).

## Problem Statement

Someone evaluating this pipeline can open the static bundle and see what it did to a corpus we picked:
the page image beside the engine's reading, the routing decision, the Chunks. What they cannot do is
bring their own PDF and watch the pipeline decide about it.

The reason is cost and shape, not capability. The probe, `OcrRoutingPolicy` and
`ExtractionStep.route/extract` already produce exactly the decision the visitor wants to see, and the
engines to act on it already exist. But the demo host is not given a GPU, and a live demo is
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
keeps nothing: no uploaded PDF, no extracted text, no page image survives the response. What it shows is
the read itself: the worker's raw output as the transport delivered it, beside the page as it would be
published -- rendered from the pipeline's own Page Text, never from the transcript (ADR-0014, the page
contract below).

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
24. As a visitor, I want to watch the OCR lane's raw output arrive as it is produced, so that I see a read happening and not only its result.
25. As a visitor, I want the page as it would be published, rendered from what the pipeline exports, so that I can judge the product and not only the process.
26. As a visitor, I want to copy that document or download its markdown, so that I can take it into a review under my own file name.
27. As a visitor, I want to be told when a lane has nothing to stream, so that an empty pane is not mistaken for a broken demo.
28. As a visitor, I want a failed read to keep the pages and the transcript it already produced, so that one failure does not erase what the worker did read.
29. As an operator, I want to deploy the OCR worker once and let it scale to zero, so that an idle demo costs storage and not GPU time.
30. As an operator, I want the deployed worker pinned to the same image, weights and detail as the batch worker, so that a Reading from the demo is comparable with a Reading from a run.
31. As an operator, I want a cold start to be a page state and never a new Engine Identity, so that the memo is not invalidated by the address or the lifecycle.
32. As an operator, I want the endpoint to refuse a request without the bearer, so that a public GPU endpoint is not a bill waiting to happen.
33. As an operator, I want the token to live in a Modal Secret and never in the page, the contract or the browser, so that rotating it is one change in one place.
34. As an operator, I want a command that reports and changes whether the worker is held warm, so that I can keep it warm for a session on purpose.
35. As an operator, I want that command to name what a warm worker costs per minute, so that leaving it on is a decision and not an accident.
36. As an operator, I want the warm control to be off by default, so that the demo's resting cost is zero.
37. As an operator, I want the demo server to report which gate it hit and how much budget is left, so that I can size the gates from real use.
38. As an operator, I want the page cap, the rate limit and the daily budget configurable, so that I can change the spend posture without a code change.
39. As an operator, I want one command that prints the address, model, detail and warm state of the deployed worker, so that wiring the demo server is copy and paste rather than derivation.
40. As an operator, I want the demo server to lease its GPU through the existing provider, so that pointing it at a leased worker is one flag.
41. As an operator, I want the demo server to keep no state on disk, so that a demo host holds nothing about anyone's document.
42. As an operator, I want a runbook that says how to check auth, deployment and the cold-start number instead of asserting them, so that the document cannot go stale.
43. As an operator, I want the raw transcript's byte ceiling and heartbeat configurable, so that I can bound a stream's memory and a quiet connection without a code change.
44. As an operator, I want the rendered document to come only from a pinned renderer and sanitizer, so that a missing extra is a stated absence and never unescaped model text.
45. As a maintainer, I want the gates to be one pure policy with one order, so that two callers cannot disagree about which gate closed.
46. As a maintainer, I want the demo's engine wiring read from the same environment names the flow reads, so that the demo and a run cannot drift into two engines.
47. As a maintainer, I want the demo to reuse `ExtractionStep` rather than re-decide routing, so that a policy change moves both or neither.
48. As a maintainer, I want the deployed app to import no vendor SDK under `src/`, so that the base install stays vendor-free (ADR-0001, ADR-0007).
49. As a maintainer, I want the demo server's API tested through HTTP with fakes, so that the honest behaviour of stages and refusals is pinned without a GPU.
50. As a maintainer, I want the deploy half tested against a fake SDK, so that the pins, the bearer and the autoscaler calls are pinned offline.
51. As a maintainer, I want a test that the demo keeps nothing, so that the retention promise in the ADR is a check and not a sentence.
52. As a maintainer, I want the glossary to name a Reading, a Spend Gate, a Gate Ledger and the Warm Control, so that tickets and code use one vocabulary.
53. As a maintainer, I want the rendered pane built from the same Page Text the corpus exports, through the corpus's one projection, so that the demo can never be prettier than the product.
54. As a maintainer, I want a test that a hostile page renders inert and that the job answer stays text, so that the sanitizer's allowlist and the data answer are checks and not sentences.

## Implementation Decisions

### The demo is three layers, and the interface layer is its composition root

- **Domain policy** (`domain/demo.py`): the limits, the usage snapshot, the gate decisions, the
  Reading stage names, and the outcome types. Pure; no I/O, no clock, no HTTP.
- **Application use case** (`application/demo_read.py`): `DemoRead.read` turns uploaded bytes and a
  client identity into one Reading. It reuses `ExtractionStep.route`/`extract_with` from
  `application/curate.py` -- routing is *the* policy and the demo must not have its own copy -- and it
  never cleans, language-tags, publishes or deduplicates.
- **Interface** (`interfaces/demo_server.py` + `interfaces/demo_jobs.py` + `interfaces/demo_job.py` +
  `interfaces/demo_page.py`, and ADR-0014's `demo_transcript.py` and `demo_render.py`): the HTTP server,
  the Reading Queue, the page asset, one Demo Job per upload -- its stage, answer, bounded raw transcript
  and document view, read by every pane -- and the document's pinned renderer and sanitizer. This is the
  composition root: it reads `FINEPDFS_*`, leases the GPU through `leased_environment`, builds the engine
  and the endpoint probe, and owns the Gate Ledger for the life of the process.

The entry point is the console script `finepdfs-live-demo` (`finepdfs.interfaces.demo_server:main`;
`python -m finepdfs.interfaces.demo_server` is the module fallback), binding `127.0.0.1:8766` by default. Its
HTTP surface is `GET /` (the page), `GET /api/status`, `POST /api/read` (the document as the body,
`Content-Type: application/pdf`, `image/png`, `image/jpeg` or `image/tiff`, answering `202 {"job": ...}`),
`GET /api/read/<id>` (the job JSON), `GET /api/read/<id>/events` (the raw transcript, as
`text/event-stream`), `GET /api/read/<id>/document` (the rendered document, as an inert HTML fragment) and
`GET /api/read/<id>/document.md` (the same pages as markdown).

A scan is a document too. A picture of a page carries no text layer, so it is exactly the case the OCR
lane exists for: the reader MuPDF gives one page per image frame (a three-frame TIFF is three pages), the
Routing Decision measures that, and the page cap and the OCR budget count those pages. WebP is
deliberately not admitted, because the reader cannot open one.

The edge checks what an edge can check before it reads a body: a media type from that list, a declared
`Content-Length`, and that length against the byte cap (`413`, kind `upload`, decided before the body is
pulled). Whether the bytes are a readable PDF is not an edge question -- the probe is the one
place that decides it, so those bytes are accepted as a job and refused a moment later with kind
`unreadable` and the probe's own words. That keeps one rule for "this is not a PDF" instead of two that
could disagree, and the visitor still does not wait for a read that cannot happen: routing happens before
the gates and before any GPU time.

Nothing under `src/` imports Modal, and nothing under `src/` imports `deploy/`.

### The page is three panes: what was sent, what came back, what would be published

ADR-0014 amends the page, not the flow: no declared state, record, page record, chunk, chunk id or search
index moves because of it. The page shows three panes in one row -- **1. What you sent** (the upload
control, the statements about the worker and retention, and the stage timeline with the measured
warm-up), **2. What came back: the raw transcript** (the worker's own output, arriving), and **3. What
would be published: the rendered document** (the page projection). A wide screen puts them side by side; anything
narrower stacks them in that order. The Routing Decision, the Engine Identity, and the refusal and
failure panels keep their wording and follow the row. The sixth user story is unchanged, and its "page
text the chosen lane actually read" is the rendered pane's own source: the pane and the corpus show the
same Page Texts, rendered.

**The raw transcript pane is the transport's verbatim output, set as text.** It holds exactly what the worker
emitted -- echoed prompt, fences, layout markers, before any normalization -- tagged with its 1-based
page because several pages are in flight at once, and the page sets it with `textContent`: a delta is
never parsed as HTML, and neither pane has a link or an image. A lane with nothing to stream (the text
layer, a local command engine) says so in words -- "this lane does not stream" -- rather than animating
a stream it never received. The pane exists to make the transformation visible; what is in it is proof
of work, never evidence, and nothing in it is exported, recorded or cited.

**The transcript is ephemeral and bounded.** One ring buffer per page, with the byte ceiling below;
a page that outruns its ceiling keeps its newest bytes and reports the drop as a `truncated` event with a
cumulative `dropped_bytes`, never as a silently shortened page. The job frees it the moment the Reading
finishes. `GET /api/read/<id>/events` answers `text/event-stream; charset=utf-8` with `Cache-Control:
no-store` and five event names -- `delta`, `truncated`, `page` (the Page Text that page exported),
`error` (kind, lane and the job answer's own redacted words) and terminal `done` -- plus a heartbeat
comment while the read is quiet. A subscriber that connects late, or reloads, is replayed from the start,
and for a truncated page that replay is the `truncated` marker first and then the retained tail, so the
loss is named before the text that survived it; a subscriber that is already watching hears the marker
where the drop happened, because that is when it happened. A browser closes the stream on `done` instead
of reconnecting. An unknown job id is `404`; a job that already finished is `410`, because its
transcript is not kept -- and with a ceiling of `0` the events endpoint answers `410` for every job,
naming that ceiling rather than a finish a queued or running read has not reached. The job answer was
always the final answer, and it never carries a transcript, a page of HTML or a `data-page`.

**The document is its own answer, so the data answer is never markup.** `GET /api/read/<id>/document`
returns one inert HTML fragment: an `<article class="document">` holding one `<section
class="document-page" data-page="N" data-state="complete|pending|unread">` per page, in the original
1-based numeric order, each complete page introduced by its `Page N` separator; a page still being read
is a `pending` skeleton and a delta never reaches it, so a half-read table cannot appear, and after a
failed read the pages it never reached are `unread` and say they were not read -- a failed read, the
queue's own breakage included, is not still reading. The page numbers are the pages the job has plus the
count routing gave it, and the last one is whichever of the two is greater: with no count, no page the
read never reported is drawn and no page after the last one reported is invented, and a page the read did
report is never cut off by a smaller count. A failure keeps the count, which names the pages it never
reached; a refusal drops it, because a read that never ran has no page to come.
`X-Document-Completed` and (once the job knows it) `X-Document-Pages` say how much of it is real. `GET /api/read/<id>/document.md`
returns the same completed pages as the markdown the corpus writes -- the one shared projection
(`document_markdown`), byte for byte -- and needs no optional dependency. The pane offers a copy button
and a `.md` download named after the uploaded file. Rendering is server-side and the page inserts exactly
one fragment, at one call site.

**Rendering is allowlisted and pinned, and absence is stated.** A markdown renderer and a sanitizer,
pinned exactly in the optional `[demo]` extra and in `docs/demo/requirements-demo.txt`, carried by the
`dev` extra too so the offline gate that runs the pane's tests installs them, and imported lazily, turn
Page Text into that fragment against a structural allowlist: headings, paragraphs, lists,
emphasis, code blocks, blockquotes, rules and tables with `colspan`/`rowspan` and nothing else -- no
`script`, `style`, `iframe`, `img` or `a`, no `on*` attribute and no URL, so model output cannot create
navigation or run. Math stays text. Without the extra the document endpoint answers `503` in words
(`rendering: not-installed`), the page says so in its own sentence, and the `.md` answer still works.
Normalization stays where it was -- one function, the adapter's `strip_model_preamble` -- so a marker an
engine leaked shows as residue in both panes: the demo contributes no second cleanup and must never be
prettier than the product.

### A Reading is an outcome, not a bool

`DemoRead.read(content, client, on_stage, name)` returns exactly one of three outcomes, all carrying the
Routing Decision when one was made:

- **read** -- the lane was chosen, the lane read it, and the outcome carries the per-page text, the
  Engine Identity, the features and thresholds, the elapsed time and (for the OCR lane) how long the
  endpoint took to answer when it was cold.
- **refused** -- the demo declined before or instead of spending: the page cap, the rate limit, the
  daily budget, an unreadable upload, or a full queue. A refusal names its kind and its reason in
  words, and carries no page text.
- **failed** -- a lane was chosen and the read failed. The reason is the underlying error's own words,
  never a summary, and the kind is the exclusion kind `failed` that already exists. A page the worker
  cut off before it was finished -- the model's own token limit above all, whether the answer arrived as
  a stream or in one blocking call -- is this outcome and not a short page: the exported Page Text is
  never the part the worker managed to send. The blocking answer a batch run reads carries the same
  `finish_reason`, so the guard is on that path too, not only on a stream. Those words are shown
  through the composition root's redactor, because an engine's transport error quotes the address it
  called and the job answer is public: no address and no bearer reaches the page. No Reading is recorded,
  because the demo records nothing anywhere.

One refusal kind exists beyond the gates and the upload itself: `unavailable`, when the demo cannot
reach an OCR worker at all -- a provider whose start command failed, a lease that was refused. No gate
closed and no lane ran.

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
read that fails still spent the GPU. It happens only once the OCR worker exists, though -- a read whose
worker would not start read nothing and spent nothing, and a refusal charges nothing, so a start command
that keeps failing can eat neither a visitor's window nor the day's budget.

### The Reading Queue is the demo server's, and it is bounded

One Reading runs at a time and a bounded number wait behind it. The single slot is what makes the
per-read environment lease safe (a lease mutates `os.environ`, which the flow's own model already
assumes is one run at a time), and it is what ADR-0012 means when it says what serialises uploads is the
demo server's queue and not Modal. A full queue is a refusal with its own kind, not an unbounded wait.
The most recent finished jobs stay answerable, so a page that polls a moment late still gets its answer;
one bound covers them, and a job past it is forgotten with everything it held -- its answer and its
document -- while its raw transcript was already freed when the Reading ended. Nothing survives a restart.

The protocol is therefore a job, not a long request: the upload answers `202` with a job id, and the page
polls that job for its stage. The stages are `queued`, `routing`, `warming`, `reading`, and the terminal
state `done` carries the outcome. This is what makes "warming the OCR worker" a real state rather than
an animation: the server is waiting for the endpoint's `/v1/models` to answer, and it reports how long
that took.

### The worker is entered for an admitted OCR read, and for nothing else

The OCR lane's worker is a context manager the composition root hands the use case: entering it yields the
engine and the probe that says whether its endpoint answers, and leaving it gives the worker back. The use
case enters it **after** the gates have let an OCR read through and **before** the warming stage, so the
order a spend can happen in is: route, decide, admit, and only then rent. A native upload rents nothing,
and a refused one rents nothing -- which is the difference between a demo and a bill.

Whether there is anything to wait for is the engine registry's own rule, not a guess: a command means a
process on this host (nothing to warm), an address means an HTTP endpoint (something to ask). An HTTP
engine with no address configured says so rather than pretending the worker is ready.

### Warming is measured, not guessed

When the Routing Decision says OCR, the use case asks the endpoint probe whether the endpoint answers.
The clock starts before that first question, because asking can itself start a container and block for
as long as the probe waits: a first check that returned after a wait is a wait, and reporting it as "no
wait" would understate the one number this demo exists to measure. If the first answer is yes the read
proceeds with no warming stage; if it is no the use case announces `warming` and polls the probe until
the warm timeout, then announces `reading`. Either way the wait travels back to the page as milliseconds
-- zero when the worker answered at once, and `null` only on the native lane, which never asks.

The probe is a port: an adapter asks the endpoint's OpenAI-compatible `/v1/models` with the bearer, and a
fake answers in tests. A probe that never answers is a failed Reading whose reason names the timeout, not
a refusal: the gate did not close, the worker did not come up.

### Only page images leave the host, and nothing is kept

The demo server opens the uploaded bytes as a document in memory -- a PDF, or an image read as pages. The OCR lane renders page images into a
scratch directory that the engine removes before the response returns -- that scratch directory is the one
way a Reading touches a disk, and both lanes' retention tests assert that nothing survives it: no uploaded
PDF, no page image, no engine result, nothing under the working directory or `TMPDIR`. Finished jobs are
kept in memory, bounded, so a page that polls a moment late still reads its answer; no job and no page text
survives a restart.

The page states, before the upload control, that the document is sent to a rented GPU to be read and that
nothing is kept after the response. Once a Reading starts, that pane also carries the stage timeline with
the measured warm-up, the two panes beside it carry the raw transcript and the rendered document as they
arrive, and the Routing Decision with its features and thresholds and the Engine Identity that produced
the text follow the row -- with a refusal or failure panel in place of the reading when there is none.

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

Every limit is a default with an environment override, so the spend posture -- and, for the transcript,
the stream's shape -- is a setting. The defaults live in `domain/demo.py` and the variables are read by
`interfaces/demo_server.py`; the operator's copy of this table, with the checks, is
[the live demo runbook](../deploy/live-demo-runbook.md):

- upload size (`FINEPDFS_DEMO_UPLOAD_BYTES`, default 20 MiB) -- refused in the handler, before the body is
  fully read;
- page cap per upload (`FINEPDFS_DEMO_PAGE_CAP`, default 25 pages) -- refused from the probe's page count;
- rate limit (`FINEPDFS_DEMO_RATE_LIMIT_READS` per `FINEPDFS_DEMO_RATE_LIMIT_WINDOW_SECONDS`, default 5 reads
  per 600 s per client) -- refused per client;
- daily OCR page budget (`FINEPDFS_DEMO_DAILY_OCR_PAGES`, default 300 pages, UTC day) -- refused when the OCR
  lane would exceed it;
- Reading Queue depth (`FINEPDFS_DEMO_QUEUE_DEPTH`, default 3 waiting) -- refused when full;
- warm timeout (`FINEPDFS_DEMO_WARM_TIMEOUT_SECONDS`, default 900 s) and poll interval
  (`FINEPDFS_DEMO_POLL_INTERVAL_SECONDS`, default 2 s) -- a read that never warms fails;
- transcript ceiling (`FINEPDFS_DEMO_TRANSCRIPT_BYTES`, flag `--transcript-bytes`, default 262144 bytes --
  256 KiB of raw output kept per page, the unit being raw bytes of the worker's text; `0` keeps no
  transcript at all and the events endpoint then answers `410`) -- what a page's ring buffer holds before it
  is marked `truncated`;
- stream heartbeat (`FINEPDFS_DEMO_HEARTBEAT_SECONDS`, flag `--heartbeat-seconds`, default 15 s of quiet,
  the unit being seconds, and it must be positive) -- how long a silent `text/event-stream` waits before
  it writes a heartbeat comment; a zero or negative interval is refused when the demo starts, because it
  would never block and the stream loop would spin.

The console script takes one flag per limit (`--page-cap`, `--rate-limit-reads`,
`--rate-limit-window-seconds`, `--daily-ocr-pages`, `--upload-bytes`, `--queue-depth`, `--warm-timeout`,
`--poll-interval`, `--transcript-bytes`, `--heartbeat-seconds`), each overriding the matching variable for
that run and replacing no other. An exported variable with an empty value means the default. The ceiling
and the heartbeat bound a stream's memory and a quiet connection; neither is charged against a Spend
Gate, and neither number has been measured against a live worker -- the runbook carries the command that
reads the running process's own values instead.

A client is the left-most `X-Forwarded-For` hop when a proxy set one, else the peer address. This is a
spend brake and not an identity: a public deployment must sit behind a proxy that sets the header, and
the daily budget and `max_containers=1` are the bounds that do not depend on it.

## Testing Decisions

A good test here drives the highest seam that can observe the behaviour and asserts what a visitor or an
operator can see: a stage, an outcome, a refusal kind and its words, a page's text and number, the
identity, a number that was measured, or a pinned contract value. It does not assert private helpers,
intermediate counters that the page never shows, or the order of statements inside a method.

The seams, highest first:

1. **The demo server's HTTP API** (`tests/interfaces/test_demo_server.py`, and for ADR-0014's two answers
   `tests/interfaces/test_demo_transcript.py`, `test_demo_document_server.py`). The whole feature through
   one seam: POST a PDF, poll the job, read the decision and the page texts; watch a refusal per kind; see
   the queue refuse a second upload while one is running. The real `ThreadingHTTPServer` runs on port 0
   with a fake probe, fake engines, a fake endpoint probe and an in-memory ledger. The stream and the
   fragment are tested through the same seam: a subscriber that reloads is replayed delta for delta, a
   disconnect is charged once, a finished job answers `410`, the golden page records come back in page
   order, and a hostile page renders inert. Prior art: `tests/interfaces/test_explorer.py`.
2. **The page asset's own JavaScript** (`tests/interfaces/test_demo_page_raw.py`,
   `tests/interfaces/test_demo_page_document.py`). The page's pure functions run under Node when Node is
   present and are skipped when it is not, fed the answers the real server produced: a delta reaches the
   raw transcript pane as text, a reload converges on the job's answer, a skeleton is replaced by its page, and the
   three panes are in the visitor's reading order. The rest -- one `innerHTML`, no second status
   vocabulary -- is a static assertion on the asset, because the suite has no browser.
3. **The use case** (`tests/application/test_demo_read.py`). `DemoRead` with hand-written fakes, the way
   `tests/application/test_curate_document.py` tests `CurateDocument`: the gate order, a cold endpoint
   announced as a stage and its wait reported, a lane failure verbatim, and the promise that a native
   read never touches the OCR engine.
4. **The gate policy and the ledger** (`tests/domain/test_demo_gates.py`, plus the ledger's own test).
   Boundary values for each gate, the order between them, the day rollover, and that a failed read is
   not refunded. Pure and fast.
5. **The deploy half** (`tests/deploy/test_modal_serve.py`, `tests/deploy/test_modal_warm.py`, and the
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
  observed. That slot is in [the live demo runbook](../deploy/live-demo-runbook.md#9-the-cold-start-slot-measuring-the-first-ocr-after-idle),
  which is also where the deploy, the bearer Secret, the Warm Control's price and the counters' known limits
  are checked rather than asserted.
- The demo deliberately shows the deployed app's `/v1` base URL as configured, not as embedded: an
  operator pastes it from the Warm Control's output, which is the only place that knows how to turn a
  bare Modal host into an OpenAI base URL.
- A leaked bearer is a bill, not an outage. The runbook names rotation as one Secret update plus one
  environment update on the demo host, and says what the page must never carry.
- The two postures sharing one set of pins is what makes the demo's numbers comparable with a run's. If
  a future change gives the deployed app its own image or model, this spec's promise that a Reading from
  the demo is comparable with a Reading from a run stops being true, and that is a decision, not a
  cleanup.
