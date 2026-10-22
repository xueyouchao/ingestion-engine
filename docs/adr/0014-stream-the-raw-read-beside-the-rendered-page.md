# 14. Show the raw read beside the rendered page in the live demo

Date: 2026-10-22

## Status

Proposed for review. Nothing in this ADR is implemented, run or measured.

If accepted it extends ADR-0012's live upload demo (the page, the job queue and the deployed Modal
worker) and amends the page contract in [`docs/spec/live-upload-demo.md`](../spec/live-upload-demo.md):
the transcript and the rendered document are two new page surfaces, and the spec's sixth user story
("the page text the chosen lane actually read") is kept as the source of the second one. It leaves
ADR-0011's static bundle untouched: a bundle cannot contain a transcript that was never declared. It
changes presentation and transport, not the flow: no declared state, record, page record, chunk, chunk
id or search index moves because of this ADR.

## Context

### What the live demo shows today

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
in a new optional `[demo]` extra and recorded in `docs/demo/requirements-demo.txt`, turn Page Text into
an HTML fragment. The allowlist is structural only: headings, paragraphs, lists, emphasis, code blocks,
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

- The buffer ceiling and heartbeat interval are not measured; they belong in the runbook once the
  endpoint exists.
- The renderer/sanitizer pair is not chosen or pinned here; the requirement is a pinned, offline-installable
  pair whose output is stable enough to golden-test.
- Whether the OCR lane can stream depends on the worker: vLLM and SGLang both serve SSE for this model
  family, but the demo's cold-start path and the page budget have not been exercised with a stream yet.
- Figures/images from a page are dropped by normalization today; showing them in the rendered pane would
  need a resolved artifact, not model-emitted `images/0.jpg` paths, and is out of scope.
- "Raw Transcript" and "Rendered Document" are names for a demo pane, not domain vocabulary. If either
  ever becomes an exported artifact, it enters `docs/GLOSSARY.md` first.
