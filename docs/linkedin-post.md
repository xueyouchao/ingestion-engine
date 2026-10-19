# LinkedIn post — finepdfs-cocoindex

## Main post

PDF ingestion is everywhere in agentic data-ingestion flows — policy PDFs, contracts, filings — and it's where those flows quietly break.

Every corpus holds three kinds of document:
→ Born-digital, with a real text layer. Cheap to read.
→ A pure image scan. Nothing to read, so a vision model has to render and read every page.
→ The one that hurts: a text layer that exists but is wrong — garbled glyphs, broken encodings, an almost-empty layer over an image. Trust it and your agent retrieves confident nonsense.

So "does this PDF have text?" is the wrong question. I built a curation pipeline shaped like FinePDFs that asks a better one, per document: can this text layer be trusted?

Per PDF:
→ Fetch real PDFs from Common Crawl (index API → one ranged WARC read)
→ Route: measure the text layer — chars per page, coverage, garbled share, image-dominated pages — then decide
→ Fall back to OCR behind a single port: PyMuPDF, Docling, PaddleOCR-VL or MinerU — page requests overlap, bounded by one run-wide in-flight knob
→ Clean — running titles, page numbers, repeated table headers, hyphenation, with different rules for native vs OCR text
→ Language-tag per page, then per document; disagreeing pages are dropped
→ Judge: min pages, min characters, language allow-list
→ Deduplicate exact (digest) and near (fingerprint) in one corpus-wide pass
→ Publish Markdown, JSON records, page-level JSONL, and an exclusion for every dropped document

The other half was cost. Incrementality is the design, not a cache on top: only the engine call is memoised, on (PDF digest, engine name, engine identity). Change a rule → only the cheap pass re-runs. Move the corpus → nothing is re-read. Change the engine → only its documents are re-read.
On 18 real NOAA PDFs: cold run 134 s, re-run 8.6 s with 0 engine calls.

Also shipped:
• GPU workers behind a provider seam — a server you run, or a Modal L4 Sandbox rented per run and handed back
• A static reader bundle: one link a stranger opens cold — page image beside the engine's reading, corpus search in the browser, and a fingerprint that flags drift from declared output
• SQLite FTS5 search over page-level chunks, where a stale index says "run index-search" instead of quietly returning fewer hits
• An `ask` agent whose citations are verified against the accepted page — a citation nobody read is stripped, not printed

Underneath: ports and adapters (~10k lines, 560 offline tests), and an ADR for every decision with the alternative that lost.

Five diagrams, outside in: architecture and seams, one run's two passes, why a re-run is cheap, search over accepted evidence, and the identity model behind stable citations.

Honest limits: 18 documents isn't corpus scale, and two of the four OCR engines are tested against recorded interfaces, not live weights.

#DataEngineering #RAG #DocumentAI #CommonCrawl #Python

---

## Short cut (~950 characters)

PDF ingestion is the most common input in agentic data ingestion — and the least forgiving. Some PDFs have a clean text layer, some are pure image scans, and the worst ones have a text layer that exists but is garbage, so your agent retrieves confident nonsense.

Built a PDF curation pipeline shaped like FinePDFs that routes each document on measured text quality, reads the untrustworthy ones with four OCR engines behind one port (PyMuPDF, Docling, PaddleOCR-VL, MinerU), then cleans, language-tags, deduplicates and publishes. Incrementality is the design: only the engine call is memoised on (digest, engine name, engine identity). 18 real NOAA PDFs: 134 s cold, 8.6 s warm with 0 engine calls.

One link shows the whole result: a static reader bundle with the page image beside the engine's reading, search running in the browser, and a fingerprint that flags when it drifts from what was declared.

#DataEngineering #RAG #DocumentAI

---

## Posting the diagrams

LinkedIn allows 20 images per post; use all five, in this order. They read well as a carousel because
each answers one question the text raises.

| Slide | File | Caption / alt text |
|---|---|---|
| 1 | `docs/diagrams/png/01-architecture.png` | Container view: the CLI drives a CocoIndex flow; four OCR engines behind one port; corpus files and declared outputs are the durable state |
| 2 | `docs/diagrams/png/02-run-pipeline.png` | One run, two passes: every document read concurrently and memoised, then a single corpus-wide deciding pass |
| 3 | `docs/diagrams/png/03-incremental.png` | Incremental by construction: memo key is (PDF digest, engine name, engine identity) — what a rule, engine or corpus change actually costs |
| 4 | `docs/diagrams/png/04-search-evidence.png` | Search over accepted evidence: one lexical index, one hit contract, one citation rule |
| 5 | `docs/diagrams/png/05-identity-model.png` | Two identities, one evidence chain: source identity vs document digest, and what a citation points at |

PNGs are 1920 px wide (above LinkedIn's 1200 px recommendation). Drop the text above the first
diagram when posting as a carousel.

## Notes before posting

- The opening frames PDF ingestion as a common input to agentic data-ingestion flows and names the three
  cases the routing actually distinguishes (native text, image-only scan, unusable text layer). That third
  case is the pitch: it is why routing exists, and it is the one most ingestion demos skip.
- Figures in the main post are the rehearsed ones from `docs/demo/pdf-demo-runbook.md` (Docling on two
  genuine scans, CPU only); the GPU claim is the four L4 Modal workers in
  `docs/deploy/modal-worker-evidence.md`.
- The `ask` agent figures in `docs/demo/agent-evidence.md` were measured on text-layer evidence, not OCR
  evidence — which is why the post says "verified against the accepted page" and quotes no accuracy number.
- One caveat on the headline numbers: the 134 s cold run in `docs/demo/pdf-demo-runbook.md` was measured
  before ADR-0010 overlapped the page requests, and the runbook has not been re-run since — so it is the
  pre-overlap figure. The 8.6 s warm rerun makes 0 engine calls and is unaffected. If you want the post to
  claim the improvement, re-run the demo cold first and quote the new number; otherwise "cold run 134 s"
  stays literally true and simply conservative.
- The `explore` browser page still exists (it needs a Python process per visitor); the bundle bullet
  describes the shareable form of the same read-only view, so nothing was dropped from the project.
- Updated 2026-10-19 for the two features merged since the first draft: ADR-0011's `finepdfs bundle`
  (the static reader bundle bullet, which replaces the plain "read-only explorer" line) and ADR-0010's
  overlapped OCR page requests under `FINEPDFS_ENGINE_CONCURRENCY` (the "page requests overlap" clause).
  Code counts refreshed: ~9.7k lines in `src/`, and 565 tests collected with 5 deselected for
  network/model, so 560 in the default offline suite (`pytest --collect-only -q`).
- The bundle figure worth trusting is the one in the feature commit: verified in Chrome against the built
  bundle, 313 of 313 page images decode and 1,184 Chunk offsets fall inside their page text. The
  `--check` fingerprint covers the run summary, records, pages, chunks and exclusions.
- Main post is ~2,870 characters, under LinkedIn's 3,000 limit but close. If you add a link, move the
  "Honest limits" line to the first comment: links add characters and some counters include the preview.
  Keep the caveat somewhere — the 18-document scale and the two interface-tested engines are the claims a
  careful reader will check.
