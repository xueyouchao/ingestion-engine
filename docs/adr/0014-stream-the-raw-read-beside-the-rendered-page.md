# 14. Show the raw read beside the rendered page in the live demo

Date: 2026-10-22

## Status

Accepted. Scope: the live upload demo's page (ADR-0012). The decisions below are implemented and pinned
offline by the tests named under [What implementation proved](#what-implementation-proved); the numbers
that no one has measured against a live worker are named there and kept in
[Open items](#open-items).

It extends ADR-0012's live upload demo (the page, the job queue and the deployed Modal worker) and amends
the page contract in [`docs/spec/live-upload-demo.md`](../spec/live-upload-demo.md):
the transcript and the rendered document are two new page surfaces, and the spec's sixth user story
("the page text the chosen lane actually read") is kept as the source of the second one. It leaves
ADR-0011's static bundle untouched: a bundle cannot contain a transcript that was never declared. It
changes presentation and transport, not the flow: no declared state, record, page record, chunk, chunk
id or search index moves because of this ADR.

## Context

### What the live demo showed before this decision

A visitor uploads a PDF or an image, the demo routes it, and `renderReading` puts each accepted page's
text in a `<pre>` beside its page number (`src/finepdfs/interfaces/demo_page.py`). The job is polled at
`/api/read/<id>` until `stage === "done"`; there is no partial answer, and `VlmChatTransport` makes one
blocking `/chat/completions` call per page (`src/finepdfs/infrastructure/engines/vlm_chat.py`).

So the page answers "what did the engine read?" in a monospaced blob, and nothing about it is bold,
tabulated or ordered. That is honest but not legible: a two-column paper comes back as line soup, and a
table comes back as annotation markup if the engine emits any. Meanwhile the reference demo that
motivated this ADR (Baidu's Unlimited-OCR Space) shows two things at once that ours collapses: the model's
raw tokens as they arrive, then the clean artifact the model wrote. Its source is small and deterministic:
`run_ocr` streams stdout, then yields `_collect_output(out_dir)`, the `result.md` the model wrote after
stripping its own `<|ref|>`/`<|det|>` markers; the page sets that text with `textContent` in a
`white-space: pre-wrap` div. No model assembles anything, and the demo does not even render markdown to
HTML.

### What "readable" actually requires

Two separate, deterministic steps, neither of which is an LLM call:

1. **Normalization** -- strip the engine's own conventions (an echoed prompt, markdown fences, layout
   markers such as `<|det|>table [x1,y1,x2,y2]<|/det|>`) and keep the text. This is what turns a token
   stream into a Page Text.
2. **Presentation** -- render that Page Text as markdown, then as HTML, for a human to read.

The first step is already partly in the pipeline (`strip_model_preamble`), and it belongs to the engine
adapter, not to the page. The second is display, and it belongs to the demo. The failure the request
describes -- "my current one only gets chunks" -- is a transport fact: a stream was consumed as if it
were a document, and the final, normalized artifact was never fetched.

### Why not an LLM at the end

An assembly pass would be lossy (it would rewrite text that citations point at), non-reproducible (the
same upload could render differently per visitor), and it would put a second model in the citation path
for a display problem. It also cannot be memoised without memoising the reading itself. Rejected.

### Shapes considered

1. **Render the page text client-side only.** Cheapest, but the raw/clean transformation is exactly the
   thing worth showing; a formatted blob alone looks like a different pipeline wrote it.
2. **Stream the raw tokens and keep the `<pre>` as the final answer.** Shows provenance, hides the
   product: nobody wants to read annotation markup.
3. **Give the demo its own markdown formatter and HTML template.** Fast to build, but it forks
   normalization from the pipeline, so the demo can show text the corpus would never publish.
4. **Two new panes beside the existing input** -- raw as the engine emitted it, rendered as the pipeline
   would publish it, from one read. This ADR.

## Decision

**One upload, one engine call, three panes: input, raw transcript, rendered document. The left pane
says what was sent, the middle what came back, the right what would be published.**

**The raw pane is the transport's verbatim output, escaped and streamed.** It is what the engine
actually emitted, including markers, before any normalization. The browser sets it with `textContent`
only; it is never parsed as HTML. It exists to make the transformation visible, not to be copied.

**The stream is opt-in, per lane, and never required.** The OpenAI-compatible transport gains a streaming
call (`"stream": true`, parsed `data:` frames, `[DONE]`, `finish_reason`) used only when a caller asks for
it; `ChatEndpoint` still only says where the server is.
The OCR lane streams tokens; the pymupdf text layer has nothing to stream and appears whole; a local
command engine (Docling, MinerU) writes process output, not page tokens, and shows no stream. The page
states which case it is in -- "this lane does not stream" -- rather than animating a fake one or
inventing a token cadence.

**A page the worker cut off is a failure, not a short page.** A streamed page ends when the worker says
it did: `stop`, or nothing named before `[DONE]`, is a whole page; any other `finish_reason` -- the
model's token limit above all -- ended the stream before the page was finished, and the read fails in
the worker's own words rather than exporting what arrived. The blocking answer a batch run reads
carries the same `finish_reason`, and is held to the same rule: a cut page never reaches the corpus.
A truncated page in both panes would be a reading the product does not have.

**The transcript is ephemeral and bounded.** The job keeps a per-page ring buffer of raw deltas (a
ceiling in bytes, truncation marked explicitly with a `truncated` event rather than silently dropping
the start), serves it over `text/event-stream` at `/api/read/<id>/events`, and frees it when the job
retires. A subscriber that connects late, or reloads, replays from the start; the endpoint is
`no-store`, sends a heartbeat, and an `error` event if the read failed. Nothing in the transcript is
written to disk, put in the job answer, or cited. The job answer stays data (`reading.pages[].text`);
the rendered document is a separate endpoint so JSON never carries HTML.

**The rendered pane is the page projection, rendered page by page when each page completes.** It is
built from the same Page Text the pipeline exports, never from the transcript. Pages keep their original
1-based numbers and appear as separators, in numeric order -- the demo reuses the pipeline's ordering
rule rather than arrival order. A page that is still streaming shows a skeleton; a half-parsed table is
not rendered. Both panes carry a copy button, and the document pane a "download .md" of the
concatenation, so the demo's output can be pasted into a review.

**Normalization is one function, shared.** The demo calls the adapter's normalization; it does not
introduce a second cleanup. If an engine leaks layout markers into Page Text (the Unlimited-OCR case),
the fix is in that engine's adapter and lands in the corpus too -- the demo must never be prettier than
the product.

**Rendering is server-side, allowlisted and pinned.** A markdown renderer plus a sanitizer, both pinned
in a new optional `[demo]` extra, carried by `dev` as well so the offline gate can run the pane's tests,
and recorded in `docs/demo/requirements-demo.txt`, turn Page Text into an HTML fragment. The allowlist is structural only: headings, paragraphs, lists, emphasis, code blocks,
blockquotes, rules, and tables with `colspan`/`rowspan`; no `script`, `style`, `iframe`, `img`, no
`on*` attributes and no links in the first cut (model output must not create navigation). Math is
rendered as text until there is a reason to ship a MathJax-class dependency. The page inserts exactly
one fragment, at one call site, and a test asserts that a hostile corpus page (`<script>`, `onerror`,
`javascript:`) comes back inert.

**The three gates are unchanged, and a disconnect is not a refund.** Budget, rate and queue gates close
before the first token. Once an OCR request has been sent the page is charged whether or not the visitor
watches it, whether or not the connection drops: the GPU has already done the work, exactly as ADR-0012
prices it.

**No LLM pass, in this or any later iteration of this pane.** If cross-page repair ever becomes real,
it is a separate, declared, opt-in step with its own evidence, not a rendering feature.

## What implementation must prove

- A recorded raw fixture, replayed through the fake endpoint, concatenates to exactly the Page Text the
  pipeline exports for the same page -- no delta lost, no marker leaking into the document pane.
- The document pane's text equals the corresponding `out/pages/<key>.jsonl` entry for the same document,
  engine and page (a golden test, not a screenshot).
- A reload mid-stream replays from the beginning and converges on the same final document.
- A job that fails mid-page emits `error`, the document pane keeps the pages already completed, and the
  failure panel still names the lane and the reason.
- The page budget is charged once per OCR page, including when the visitor closes the tab mid-stream.
- A hostile page cannot execute script through the rendered pane.

## What implementation proved

Recorded on 2026-10-22 against `feature/adr-0014-raw-read` (tickets 33-37). Every proof is an offline
test: a fake endpoint, a fixture, or the demo's own `ThreadingHTTPServer` on port 0. **No test in this
list ran against a deployed worker**, and nothing below is a latency or a byte count observed on a live
stream.

| What this ADR required | The test that proves it, by name |
| --- | --- |
| A recorded raw fixture, replayed through the fake endpoint, concatenates to exactly the Page Text the pipeline exports -- no delta lost | `tests/infrastructure/test_vlm_streaming.py::test_the_recorded_page_streams_to_exactly_the_page_text_the_adapter_exports`, over `tests/fixtures/paddleocr_stream_page.sse` through `tests/fake_streaming_worker.py` |
| The document pane's text equals the `out/pages/<key>.jsonl` entry for the same document, engine and page (a golden test, not a screenshot) | `tests/interfaces/test_demo_document_server.py::test_the_document_carries_the_page_records_bytes_in_page_order_with_page_separators`, over the hand-written records in `tests/fixtures/pages/ba02d52504f527bb.jsonl` |
| A reload mid-stream replays from the beginning and converges on the same final document | `tests/interfaces/test_demo_page_raw.py::test_a_reload_mid_stream_replays_from_the_start_and_converges_on_the_jobs_answer` and `tests/interfaces/test_demo_transcript.py::test_the_real_server_streams_the_raw_read_and_a_reload_replays_it_from_the_start` |
| A job that fails mid-page emits `error`, the document pane keeps the pages already completed, and the failure panel still names the lane and the reason | `tests/interfaces/test_demo_transcript.py::test_a_failed_read_ends_the_stream_with_its_kind_lane_and_redacted_words`, `tests/interfaces/test_demo_document_server.py::test_a_failed_read_keeps_the_pages_that_already_completed`, `tests/interfaces/test_demo_page_document.py::test_a_failed_read_keeps_the_completed_pages_and_the_failure_names_the_lane` |
| The page budget is charged once per OCR page, including when the visitor closes the tab mid-stream | `tests/interfaces/test_demo_transcript.py::test_closing_the_connection_mid_stream_charges_exactly_one_read_and_its_pages`, and for a read of several pages `::test_an_admitted_multi_page_ocr_read_charges_exactly_its_pages_once` and `::test_a_multi_page_read_disconnected_mid_stream_still_charges_every_page_once` |
| A hostile page cannot execute script through the rendered pane | `tests/interfaces/test_demo_render.py::test_a_hostile_page_renders_inert_markup_only`, `tests/interfaces/test_demo_document_server.py::test_a_hostile_page_renders_inert_and_the_job_json_still_carries_text_not_html`, and for the raw pane's escape `tests/interfaces/test_demo_page_raw.py::test_a_hostile_delta_reaches_the_raw_pane_as_text` |

**"Recorded raw fixture" means the wire shape, not a capture.** `tests/fixtures/paddleocr_stream_page.sse`
is hand-written in the shape vLLM sends for this model family -- `data:` frames, the echoed prompt and the
fence split across frames, then `data: [DONE]` -- and replayed by a fake worker on `127.0.0.1:0`
(`tests/fake_streaming_worker.py`). It is not a capture from a deployed worker, so what is proven is that
this wire shape concatenates and normalizes to the exported Page Text; a live worker's own frames are
still unobserved (see [Open items](#open-items)). The golden page records are hand-written for the same
reason (`tests/fixtures/README.md`): their test proves the demo's projection of a given set of Page Texts,
not that those texts came from a run.

**"No marker leaking into the document pane" needed the code's reading, and it is narrower than it
sounds.** Normalization is `strip_model_preamble` and nothing else: the recorded page's echoed `OCR:`
prompt and markdown fence are gone from the exported Page Text, which the first test asserts by name.
Layout markers (`<|det|>`, `<|ref|>`) are **not** stripped by anything, so an engine that emitted them
would show them in both panes -- by design, since the fix belongs to that engine's adapter and lands in
the corpus (the Decision's "normalization is one function, shared"). That the demo renders residue as
residue rather than cleaning it is pinned by
`tests/interfaces/test_demo_render.py::test_marker_residue_renders_as_the_residue_it_is` and
`tests/interfaces/test_demo_document_server.py::test_marker_residue_renders_as_the_residue_and_the_markdown_keeps_it`.

**What is pinned, and what that word does not cover.** The renderer/sanitizer pair is
`markdown-it-py==4.2.0` and `nh3==0.3.7`, exact pins in the optional `[demo]` extra
(`pyproject.toml`), the same pins in the `dev` extra so CI's `pip install -e '.[dev]'` can run the
pane's tests, and in `docs/demo/requirements-demo.txt`; imported lazily so the base install never
needs them. "Pinned" means the same Page Text renders to the same bytes on the pinned pair, which is what
makes the golden and hostile tests meaningful; a bump is a reviewed change with the pane's evidence. It
does **not** mean a live worker's Page Text has been through it -- every fragment these tests render comes
from a fixture or a fake.

**The two defaults that are now real.** The demo ships a transcript ceiling of **256 KiB of raw bytes per
page** (`FINEPDFS_DEMO_TRANSCRIPT_BYTES`, flag `--transcript-bytes`; `0` keeps no transcript and the
events endpoint then answers `410`) and a heartbeat of **15 seconds of quiet**
(`FINEPDFS_DEMO_HEARTBEAT_SECONDS`, flag `--heartbeat-seconds`). Both are fields of `DemoLimits`
(`src/finepdfs/domain/demo.py`), both appear in `GET /api/status` under `limits`, and both are listed and
checked in the operator's two documents: the table in
[`docs/spec/live-upload-demo.md`](../spec/live-upload-demo.md#the-demo-servers-own-limits) and the check
in [`docs/deploy/live-demo-runbook.md`](../deploy/live-demo-runbook.md#13-the-raw-transcripts-two-bounds-and-how-to-check-them).
Neither number is measured against a live worker: both are the shipped defaults, exercised by tests
against a fake that streams on `127.0.0.1:0`, and nothing has streamed through the ceiling or waited out
the heartbeat on a real read.

## Consequences

+ The demo shows the thing the pipeline actually is -- a read plus a projection -- instead of asking the
  visitor to trust a formatted blob. The raw pane is proof of work; the right pane is the product.
+ Normalization stays in one place, so the demo cannot drift from the corpus and a bug found on stage is
  a bug in the engine adapter, fixed once.
+ No new declared state, no schema change, no memo key change, no citation change. The static bundle is
  untouched, and a transcript can never be mistaken for evidence.
+ The reference demo's punchline is reproducible without its archaeology: we ask the endpoint to stream
  instead of intercepting a model's stdout, so it works for any OpenAI-compatible worker.
- Two more surfaces to defend: an SSE endpoint that holds a connection open, and an HTML fragment built
  from untrusted text. The sanitizer becomes a security-relevant dependency and needs its pin and its
  hostile-input tests.
- Memory now grows with the stream: a ring buffer per active job, bounded but non-zero, on a demo server
  that ADR-0012 deliberately kept stateless. Truncation is visible, but a very long page will show a
  truncated transcript beside a complete document.
- Streaming adds a transport path that batch runs do not use, so the demo exercises code the pipeline's
  tests do not cover unless the fake endpoint tests are shared. Flaky SSE handling is now a demo outage.
- Markdown rendering will expose engine output that `<pre>` politely hid: odd headers, broken tables and
  marker residue become visible. That is the point, but it will make some readings look worse, and the
  demo must not "fix" them by formatting.
- Per-page rendering means the right pane appears in steps, not smoothly; a single-page upload still
  shows only at the end. That is the honest shape -- a page is the unit of a reading.

## Open items

- **The buffer ceiling and the heartbeat are defaults, not measurements.** They exist now -- 256 KiB of
  raw bytes per page and 15 quiet seconds -- as the two demo limits named above, and the runbook carries
  the command that reads them out of a running demo. No live worker has streamed through either, so
  neither number has been sized from evidence: a page longer than 256 KiB will truncate, and how often
  that happens is unknown until a real reading is watched.
- **The renderer/sanitizer pair is pinned, and pinned is all it is.** `markdown-it-py==4.2.0` and
  `nh3==0.3.7`, exact pins in `[demo]`, `dev` and `requirements-demo.txt`, with the fragment
  golden-tested and the hostile input inert. No live worker has exercised the pair: every Page Text it has rendered came from a
  fixture or a fake, so engine output that is odd in a way no fixture is (a malformed table, an unclosed
  fence) has not been seen through it.
- **Whether the OCR lane can stream depends on the worker**: vLLM and SGLang both serve SSE for this model
  family, and the streaming transport is tested against a fake that speaks that wire shape. The demo's
  cold-start path and the page budget have still not been exercised with a live stream -- the cold start
  slot in the runbook stays unmeasured, and nothing has watched a stream run across a container's
  scaledown window or a worker's own failure mid-page.
- **Figures/images** from a page are dropped by normalization today; showing them in the rendered pane
  would need a resolved artifact, not model-emitted `images/0.jpg` paths, and is out of scope.
- **"Raw Transcript" and "Rendered Document" are names for a demo pane, not domain vocabulary.** No
  exported artifact carries either name; `docs/GLOSSARY.md` now says so beside the demo's other terms. If
  either ever becomes an exported artifact, it enters the glossary first.
