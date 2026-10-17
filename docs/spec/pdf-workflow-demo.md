# Ticket: demonstrate incremental PDF curation, then add browser and agent search

- **Status:** planned; the checkboxes below describe work to do, not verified capabilities.
- **Planning date:** 2026-10-17.
- **Time budget:** two implementation days. Ingestion is the release gate; extensions are cut when it is at risk.
- **Reviewed baseline:** `255ca7632e8d6cc4c3702890cf92ec24e6172952`.
- **Related specification:** [FinePDFs integration](finepdfs-integration.md).

## Outcome

Demonstrate a FinePDFs-style workflow on CocoIndex using real Common Crawl PDFs: inspect the
routing decision, extract native text and a genuine scan, clean and language-tag the text, explain
exclusions and duplicates, and show that a rerun reuses expensive Extraction. Then expose the
result through a small browser explorer. If the core is stable, add single-node Elasticsearch
search and let an agent search the same records.

This is an implementation ticket rather than an accepted ADR. It sequences a demo and proposes
extensions; it does not replace the existing architecture decisions or claim a faithful reproduction
of the entire upstream FinePDFs pipeline.

## Existing foundation and gaps

The baseline already has a CocoIndex v1 app, separate Fetch, Extraction memoized on digest and
Engine Identity, OCR Routing, Cleaning, language adapters, corpus-wide Deduplication, declared
Markdown/record/exclusion outputs, and offline acceptance tests. Extend these seams.

The baseline README's verification section says real Docling, PaddleOCR-VL and MinerU execution
remains unverified: their HTTP and CLI tests use a stub server or recording runner. A real OCR
result is the first implementation milestone.

Two code findings need focused fixes before presenting the corresponding behavior:

1. [CurationStep.curate](../../src/finepdfs/application/curate.py) computes accepted/dropped pages,
   but Publish Policy, Markdown and character counts still consume all cleaned pages. Publish and
   index only accepted pages, keeping their original 1-based PDF page numbers.
2. [publish_corpus](../../src/finepdfs/infrastructure/cocoindex/flow.py) skips exact duplicates
   before building the dedup plan, so their exclusions exist but they do not reach
   `dedupe/duplicates.jsonl`. Include exact decisions in the declared report using the same
   deterministic keeper decision.

Published Markdown joins pages without retaining their boundaries. Search needs a page/chunk
export from accepted Page Texts; reconstructing page citations from the joined Markdown is unsafe.

## Priority and delivery gates

| Priority | Deliverable | Gate |
| --- | --- | --- |
| P0 | Real ingestion and incremental behavior | Required for the demo |
| P1 | Small browser explorer | First presentation extension after P0 works |
| P2 | Single-node Elasticsearch search | Start only after P0 is stable |
| P3 | Agent search tools | Start only after P2 returns correct, fresh results |
| Later | Handwired/WebGPU panel, hybrid search, reranking | Outside the two-day critical path |

At the end of day one, freeze the ingestion path and working environment. Day two starts with the
browser view, then search if time permits. Reserve the final half-day for acceptance checks and
rehearsal; cut optional work rather than leaving the core unrehearsed.

## P0: make the ingestion demo reproducible

- [ ] Select a fixed corpus of approximately 15-25 real Common Crawl PDFs, including native-text
  Documents and at least one genuine scan with manually checked text on known pages. Commit the
  manifest and restoration instructions, not PDF bytes or model weights. Record Crawl Records,
  WARC ranges, content digests and byte counts; replay the manifest instead of querying the latest
  crawl during the demo.
- [ ] Cover exact/near duplicates, a mixed-language document and a bad input. Prefer real examples;
  retain controlled negative fixtures separately and label their provenance. Do not present a
  corrupted derivative or synthetic fixture as evidence of extraction quality on real PDFs.
- [ ] Validate one real OCR Engine. Try Docling for the CPU profile, or PaddleOCR-VL/MinerU when a
  working GPU Endpoint is available. PyMuPDF reads existing text and is not a scanned-PDF OCR
  fallback. Validate actual output and page mapping, not only a successful process or HTTP status.
- [ ] Check the target environment and record the commands and results: Python/package versions,
  model/tool version or revision, actual endpoint/model configuration, and hardware. Use
  `python --version`, `python -m pip show cocoindex pymupdf`, the selected tool's version command,
  and `nvidia-smi` when evaluating a local NVIDIA profile. Never assume a GPU, installed model,
  authenticated provider or healthy service from this document.
- [ ] Pin the working dependencies and stable Engine Identity. Keep secrets, temporary hostnames
  and lease IDs out of committed evidence and memo identities; follow ADR-0005 and ADR-0007.
- [ ] Fix accepted-page publication and the exact-duplicate report gap above, with focused regression
  checks through published outputs.
- [ ] Select fastText when presenting language labels, or label the default script tags as scripts.
  Keep the allow-list in the vocabulary of the selected Language Mode.
- [ ] Export accepted pages, and chunks when search is enabled, as declared target state. Include
  stable `doc_id`, source URL, PDF digest, original 1-based page number, text and Engine Identity.
  A chunk additionally needs a deterministic `chunk_id`, chunk ordinal or offsets, and a
  chunking-version field. Preserve the document identity across content replacements and make
  stale page/chunk removal explicit.
- [ ] Produce a compact per-run summary: source Documents, native/OCR routing totals, actual
  extraction calls, reused extractions, published Documents, exclusions by kind, and elapsed time.
  Distinguish observed extraction reuse from other CocoIndex component statistics.
- [ ] Add a repeatable demo command or script with isolated demo state/output directories.
  Unchanged and policy-change reruns must reuse the baseline's CocoIndex state/cache. Preserve the
  pinned corpus and the working warm cache for rehearsal.

A configured `dry-run` currently curates in process and can invoke real OCR without CocoIndex
extraction memoization. Document this behavior in the runbook; do not present it as a free,
probe-only cost estimate.

The baseline async extraction function calls synchronous engine code directly. Measure wall time on
the chosen corpus before promising parallel OCR. Add bounded offload only if latency requires it and
the selected adapter's thread/process safety has been checked.

### P0 acceptance

- [ ] A native-text PDF takes the expected route, and a genuine scan produces manually verified text
  on the correct original pages using the real selected OCR Engine.
- [ ] Dropped language pages are absent from published text and page/chunk exports; text counts
  match the accepted content.
- [ ] Exact duplicates appear in the exclusion and duplicate report with the same keeper; the keeper
  remains published. Near-duplicate behavior is visible and deterministic.
- [ ] An unchanged rerun makes zero actual extraction/OCR calls. A Cleaning, Language Policy or
  Publish Policy change reuses Extraction when it does not change the selected Engine.
- [ ] Adding a Document extracts only newly needed content. Replacing bytes through a valid updated
  manifest changes the output and removes obsolete pages/chunks. Digest mismatches fail visibly.
- [ ] Removing a Crawl Record from the active manifest/source set removes its declared outputs.
  An unavailable remote URL is not treated as an intentional source deletion.
- [ ] Removing a duplicate keeper deterministically promotes a surviving representative and keeps
  declared outputs and, when enabled, search results consistent.
- [ ] One bad Document becomes an Exclusion while other Documents complete.
- [ ] Store commands, versions, timings and a small set of inspected results in a demo runbook.
  Report failures and limitations; do not reuse the historical synthetic benchmark as current
  pipeline performance evidence.

## P1: a small browser explorer

- [ ] Load run summaries, published records, pages and exclusions through a small local backend.
  A status table, filtering, document detail and a text/PDF inspection view are sufficient.
- [ ] Show source URL, language/script, Engine Identity, routing reason, page counts and exclusion
  reason. Link to the locally retained PDF at its original page.
- [ ] Refresh after a completed update. Polling a run summary is enough; streaming progress and
  live partial results are not required.
- [ ] Display only measured counters. The browser is a view over pipeline outputs and does not
  introduce a second writer or a second Deduplication decision.

## P2: single-node Elasticsearch search

Proposed deployment, all on one host except an optional model Endpoint:

```text
Common Crawl PDFs -> CocoIndex curation -> accepted page/chunk records
                                               |
                                    Elasticsearch target adapter
                                               |
                                    single-node Elasticsearch
                                               |
                                        shared search API
                                         /           \
                                  browser explorer   agent tools

CocoIndex run summaries and exclusions ------------> browser explorer
```

- [ ] Start with a pinned single-node Elasticsearch development deployment and lexical BM25 search,
  metadata filters and highlighted passages. No embeddings are required for this gate.
- [ ] Implement a small CocoIndex target adapter with deterministic IDs, idempotent bulk upserts,
  per-item error handling and deletion on non-existence. Replacements, fewer chunks, changed
  chunking, exclusions, and removed Documents must retract every obsolete indexed chunk.
- [ ] Index only Documents retained by the current curation and Deduplication decisions.
- [ ] Give the browser and agent the same backend search contract. Return `doc_id`, `chunk_id`,
  source URL, PDF digest, original page references, snippet/text and score.
- [ ] Check results after updates become search-visible; retries and reruns must not create duplicate
  hits. Test add, replacement and removal through the search API, not only by counting index rows.

CPU/GPU are deployment choices for Extraction and optional embedding/reranking inference. Keep the
record schema and search API the same. Elasticsearch's GPU feature accelerates HNSW graph construction
and has subscription/platform requirements; it is not needed for this small demo. If semantic search
is added later, use the same embedding model/revision for documents and queries and supply vectors
through the existing search contract.

## P3: agent search

- [ ] Expose `search_pdfs(query, filters, top_k)` and `read_pdf_page(doc_id, page_number)`.
  Page reads use the same accepted text and original numbering as the search records.
- [ ] Let the agent refine a query and inspect evidence, with a bounded tool-call budget and visible
  tool calls. Return a clear unsupported/not-found result for absent or excluded pages.
- [ ] Prepare five answerable questions with known supporting pages and one unanswerable question.
  Answers cite document/page evidence; the unanswerable case reports insufficient evidence.
- [ ] Verify that a removed or replaced Document cannot supply stale evidence.

## Deferred work and Handwired fit

The browser explorer is useful independently of Handwired. Its current implementation is
[`src/json-search`](https://github.com/chenglou/handwired/tree/main/src/json-search);
`lab/json-search` is historical experimentation. It performs regex matching over JSON and the
shipped configuration reads the first 512 Unicode code points of each record. Its CPU/GPU switch
means generated JavaScript versus browser WebGPU.

Borrow its document-map and highlighting presentation if useful. A future Handwired panel may search
bounded metadata/previews with a link to full records, explicit truncation indicators, and its MIT
notice preserved when reusing source. It must not silently truncate full PDF retrieval or become a
second search backend in this two-day effort.

Also defer trained OCR routers, new quality classifiers, per-language calibration, GPU HNSW indexing,
distributed processing, a new GPU-provider SDK, multi-engine comparisons and embedding/reranking
benchmarks. These can have separate tickets once the demo establishes the need.

## Rehearsal

1. Restore the pinned corpus and show its manifest and routing decisions.
2. Process native text and a real scan; inspect the PDF and extracted page together.
3. Show published content, a duplicate and a failed Document with their reasons.
4. Rerun unchanged and show zero actual extraction calls.
5. Change a cheap policy and show reused Extraction with updated declared output.
6. If search is enabled, search in the browser and inspect the cited source page.
7. Add, replace and remove an entry; show correct outputs and, if enabled, fresh search results.
8. If agent tools are enabled, show a query refinement and cited answer, then an unanswerable case.

## References

- [Glossary](../GLOSSARY.md) and [original specification](finepdfs-integration.md).
- [ADR-0001: Engines and language adapters](../adr/0001-engines-are-ports.md).
- [ADR-0002: Real Common Crawl corpus](../adr/0002-real-common-crawl-corpus.md).
- [ADR-0003: Fetch then process](../adr/0003-fetch-then-process.md).
- [ADR-0004: Heuristic routing](../adr/0004-routing-heuristic-not-trained-router.md).
- [ADR-0005: Extraction memo boundary](../adr/0005-memoise-the-engine-call-only.md).
- [ADR-0006: One duplicate decision in the flow](../adr/0006-decide-duplicates-in-the-flow.md).
- [ADR-0007: GPU provider and stable deployment identity](../adr/0007-rent-the-gpu-behind-a-provider.md).
- [CocoIndex custom targets](https://cocoindex.io/docs/advanced_topics/custom_target_connector/).
- [Elasticsearch single-node development deployment](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/install-elasticsearch-docker-basic).
- [Elasticsearch external vectors](https://www.elastic.co/docs/solutions/search/vector/bring-own-vectors).
- [Elasticsearch GPU indexing](https://www.elastic.co/docs/solutions/search/vector/gpu-vector-indexing).
- [Handwired JSON search scope and measurements](https://github.com/chenglou/handwired/blob/main/src/json-search/README.md).
