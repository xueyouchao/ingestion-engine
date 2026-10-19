# Spec: FinePDFs-style PDF curation on CocoIndex

- **Slug:** finepdfs-cocoindex
- **Tracker:** local files, deliberately outside the repository. No issue tracker is configured for
  this workspace, so the spec lives here and the ticket slices stay on the author's machine in
  `.scratch/finepdfs-cocoindex/issues/` (gitignored, never committed). If a tracker is configured
  later, publish these as issues.
- **Vocabulary:** docs/GLOSSARY.md. **Decisions of record:** docs/adr/.

## Problem Statement

I want to build a clean, language-tagged text corpus out of real PDFs that Common Crawl already
collected, using the processing shape FinePDFs uses (crawl -> screen -> OCR routing and extraction ->
clean -> language identification -> dedup -> publish), and I want to run it **incrementally**: when I
change a cleaning rule, add ten PDFs, or switch the OCR engine from Docling to PaddleOCR-VL, I should
not have to re-process everything I processed before.

Today this workspace has a benchmark, not a pipeline. It processed locally generated PDFs and a
handful of arXiv PDFs through one hardcoded extractor, wrote markdown, and had no corpus, no OCR
routing, no language identification, no dedup, no engine choice, and no incremental story beyond
CocoIndex's own memoization. The generated PDFs are not evidence of anything: they were synthetic
files built to make a benchmark run, and they must go, because the real source of truth is Common
Crawl.

## Solution

A CocoIndex v1 application plus a small domain library.

I declare the corpus (a Common Crawl index query: crawl id, domain, mime, status, limit) and the
target state I want (one markdown file and one JSONL record per published document). CocoIndex keeps
that target state in sync: it reprocesses only the documents whose crawl record changed, and deletes
outputs when a document leaves the corpus.

PDFs are fetched the way FinePDFs fetches them: the Common Crawl index gives a WARC file name, a byte
offset and a length, and the pipeline issues a single HTTP range request and parses the WARC record.
No WARC shard is ever downloaded whole.

Extraction is routed: a PDF whose embedded text layer looks healthy takes a cheap native path; a PDF
that looks scanned, garbled or image-only is routed to an OCR engine. The engine is a port with four
adapters, chosen per run: **pymupdf** (native text layer, fast, no models), **docling** (local, CPU or
GPU), **paddleocr-vl** and **mineru** (heavy document VLMs reached over an OpenAI-compatible HTTP
endpoint, so the models can live on a GPU host while the pipeline runs anywhere).

Every published document records which engine produced it, its language, and its dedup status, so
quality and cost can be compared across engines later.

## User Stories

1. As a dataset engineer, I want to declare a Common Crawl corpus query (crawl id, domain, mime,
   status, limit) so that my corpus comes from the same source FinePDFs uses.
2. As a dataset engineer, I want PDF bytes fetched with one HTTP range request built from the index
   file name, offset and length, so that a 1 GB WARC shard never lands on my disk.
3. As a dataset engineer, I want the fetched WARC record parsed and its payload verified to be a PDF,
   so that a truncated or mislabelled record fails loudly for that record only.
4. As a dataset engineer, I want a fetch step separate from processing, so that my corpus is a real
   directory of PDF files I can inspect, count and re-use.
5. As a dataset engineer, I want a manifest of the corpus (url, crawl id, content digest, size), so
   that I can see what I actually have.
6. As a dataset engineer, I want the pipeline to process the corpus directory, so that processing does
   not depend on a live crawl query mid-run.
7. As a dataset engineer, I want a healthy native text layer to skip OCR entirely, so that GPU time is
   spent only on documents that need it.
8. As a dataset engineer, I want the routing decision to be explainable (the features measured, the
   thresholds applied, the reason), so that I can tune it instead of guessing.
9. As a dataset engineer, I want to select the OCR engine per run, so that the same pipeline can run
   cheap on CPU today and heavy on a GPU host tomorrow.
10. As a dataset engineer, I want the engine name and its version/config recorded per document, so
    that two runs with different engines are distinguishable after the fact.
11. As a dataset engineer, I want heavy VLM engines addressed by base URL, model name and API key
    through an OpenAI-compatible endpoint, so that the pipeline host needs no GPU and no 40 GB
    download.
12. As a dataset engineer, I want local CLI engines (Docling, MinerU) supported through the same port,
    so that air-gapped and CPU-only runs are possible.
13. As a dataset engineer, I want to add a new engine by writing one adapter and registering it, so
    that the next best OCR model is an afternoon, not a refactor.
14. As a dataset engineer, I want extraction memoized on the PDF content digest plus engine identity,
    so that switching engines re-extracts, but changing a downstream rule does not.
15. As a dataset engineer, I want per-branch cleaning (native text versus OCR text), so that OCR
    artifacts such as repeated headers, running titles and broken hyphenation are handled where they
    occur.
16. As a dataset engineer, I want per-page language identification and a document-level language, so
    that I can filter and publish by language.
17. As a dataset engineer, I want a language policy with an explicit threshold, so that uncertain
    pages are dropped rather than guessed, and the drop is visible.
18. As a dataset engineer, I want a publish policy (minimum pages, minimum characters, language
    required), so that empty or useless documents never reach the corpus.
19. As a dataset engineer, I want exact duplicates detected by content digest, so that the same PDF
    crawled from ten mirrors counts once.
20. As a dataset engineer, I want near-duplicates detected by MinHash, so that re-issued documents
    with small differences do not inflate the corpus.
21. As a dataset engineer, I want every document's dedup outcome recorded (kept, exact duplicate of X,
    near-duplicate of X at similarity S), so that dropping is auditable.
22. As a dataset engineer, I want a document the pipeline drops to still be recorded in an exclusion
    list with its reason, so that nothing disappears silently.
23. As a dataset engineer, I want adding PDFs to the corpus to process only the new ones, so that a
    daily top-up is minutes, not hours.
24. As a dataset engineer, I want deleting a PDF from the corpus to delete its published outputs, so
    that the target directory never drifts from the corpus.
25. As a dataset engineer, I want to change a cleaning rule and have only cleaning re-run, so that
    expensive extraction is not repeated.
26. As a dataset engineer, I want a dry run that reports what would need OCR, so that I can size a GPU
    job before buying it.
27. As an operator, I want one failing document (network error, unparseable PDF, engine error) to be
    reported and skipped while the run continues, so that a long run is not lost to one bad input.
28. As an operator, I want per-run counters (fetched, routed to OCR, published, dropped, failed), so
    that I can tell success from silence.
29. As a maintainer, I want the pipeline use case expressible against ports and testable with fakes, so
    that behaviour is testable with no network, no GPU and no models.
30. As a maintainer, I want the two risky protocol mappings (WARC record -> PDF bytes, VLM response ->
    pages) to be pure functions with fixture tests, so that protocol drift is caught without a live
    service.
31. As a maintainer, I want the domain vocabulary written down in GLOSSARY.md, so that the next person
    (or agent) names things the same way.
32. As a maintainer, I want the architectural decisions recorded as ADRs, so that reviewers do not
    re-litigate why engines are ports or why the trained router model is absent.
33. As a reviewer, I want to run the whole thing with one command against real Common Crawl PDFs and
    see real output files, so that I can check the claim rather than trust it.
34. As a reviewer, I want the synthetic PDFs and their outputs gone from the repository, so that no
    reader mistakes benchmark scaffolding for the pipeline.
35. As a compliance-minded engineer, I want the licence of each engine adapter and of the corpus
    documented, so that I know what I may redistribute.

## Implementation Decisions

**Layering.** Three layers with dependencies pointing inwards only: domain (value objects, policies,
ports; no I/O, no CocoIndex), application (one pipeline use case orchestrating ports), infrastructure
(adapters: Common Crawl, engines, language, dedup, sinks, plus the CocoIndex wiring). An interfaces
layer provides the CLI. The CocoIndex wiring is infrastructure, because CocoIndex is a target-state
engine, not the domain.

**Ports** (the seams the application layer sees): CrawlSource (index + ranged reads, used by the fetch
step), TextLayerProbe, OcrEngine, LanguageIdentifier. Everything else is a pure function or a value
object.

*Amended during implementation.* The spec originally listed PdfSource, DuplicateIndex and DocumentSink
as pipeline ports. Three changes, each recorded in an ADR:

* PdfSource became CrawlSource, and it belongs to the fetch step rather than the pipeline: the corpus is
  a directory of files by the time curation runs, so the pipeline never needs the network.
* DocumentSink disappeared. In a target-state engine the flow *declares* outputs (docs, records,
  exclusions) instead of writing them, which is what makes deletion propagate. A sink port would have
  been a second way to write the same files.
* DuplicateIndex split in two, because the two kinds of duplicate are different problems (ADR-0006):
  exact duplicates are a pure function of the manifest, computed inside the flow; near-duplicates are a
  corpus-wide pass with its own command.

A fifth port, TextLayerProbe, was added: measuring the text layer needs a PDF library, and keeping that
out of the domain is what lets the routing policy be tested with plain numbers.

**Domain policies** (pure, no I/O): TextLayerQuality (measures the text layer and produces the routing
features), OcrRoutingPolicy (features -> decision + reason), CleaningRules (per-branch, ordered),
LanguagePolicy (per-page tags -> document language + accepted pages), PublishPolicy (accepted,
dropped, or excluded, with a reason), DuplicateKey (content digest) and SimilarityFingerprint
(MinHash).

**Engine adapters share what can honestly be shared.** PaddleOCR-VL is served by vLLM behind
/v1/chat/completions, so it goes through one small OpenAI-compatible transport (page image as a data
URL, prompt, response parsing). MinerU's HTTP API is a different shape -- a multipart POST to
/file_parse that answers with a content list -- so it gets its own client rather than a fiction of a
shared one. What all four engines do share: a page renderer for vision models, and one subprocess
runner (no shell, bounded time, captured output, clear error when the tool is missing) for the three
CLI-capable engines.

**Engine identity is part of the memo key.** Extraction is memoized on the PDF content digest *and* the
engine identity, so switching engines -- or the model, endpoint, page DPI or tool version behind one --
re-extracts, moving or renaming the corpus re-reads nothing, and changing a downstream rule re-runs only
the cheap half. The digest is checked against the bytes on disk, so a file that no longer holds what its
name says fails visibly instead of serving stale text.

**CocoIndex wiring.** One component per document, mounted with mount_each over a keyed iterable of
crawl records, with a stable key derived from the document URL. Expensive extraction uses memo=True.
Outputs are declared through the localfs target so deletion propagates. Document-level concurrency,
retries and failure accounting use CocoIndex's own facilities (map, exception handlers, stats) rather
than a hand-rolled queue of documents: the memoised body is synchronous, so each Document runs on a
thread of its own and its engine call blocks only itself. The only other bound is the run's one
engine-call bound, held at the leaf where the call happens -- one page request, or one local command --
and never a permit a Document takes before waiting for its own pages (ADR-0010).

**Duplicates are decided in the flow, not by a command.** An earlier version of this design had a
`dedupe` command that rewrote records and deleted Markdown after the fact. That made target state a lie:
the next `run` re-declared each record as unique and recreated each deleted file. The flow now reads
every document in a concurrent first pass and decides the whole corpus in a second pass that sees every
text at once, declaring either a document's outputs or its exclusion, plus the dedup report. Recorded in
ADR-0006.

**Outputs.** Per published document: `out/docs/<key>.md` (the cleaned text) and `out/records/<key>.json`
(url, content digest, engine, routing decision and reason, language, page counts, character count, dedup
status). Anything dropped or failed becomes `out/excluded/<key>.json` with its kind and reason. `<key>`
is the first 16 hex digits of SHA-256 of the URL.

*Amended during implementation.* The spec said one JSONL record file. Append-to-one-file is a side
effect, and side effects fight target-state deletion (the file would have to be rewritten by whichever
component ran last). One file per document keeps every output a pure target. The dedup report, which is
genuinely corpus-level, is JSONL under `out/dedupe/`.

**Corpus on disk.** The corpus is a real directory of PDF files named by content digest, plus a
manifest JSON. Fetching is a separate, resumable CLI step, so processing never depends on the network.

**CLI.** fetch-corpus, restore-corpus, inspect, dry-run, run. `fetch-corpus` asks the index for a
crawl's PDFs; `restore-corpus` replays a committed manifest and verifies every pinned digest instead of
querying the latest crawl. `dry-run` curates the corpus in process and prints a table of what would
happen without writing anything, which is the fastest way to see routing decisions on real documents.
The CocoIndex application is also runnable directly through `cocoindex update main.py`.

*Amended during implementation.* The remote engines were originally reached only at a `--base-url` an
operator supplied by hand. A rented GPU answers on a new host every run, and the memo key is the engine
identity (ADR-0005), so an address in that key would re-read the whole corpus every time. There is now a
GPU provider seam (`infrastructure/compute/`) that leases an endpoint for the length of a run and reports
a stable detail, with `none`, `static` and `command` providers; Modal and RunPod are a command line
today and an adapter behind the same seam later. Recorded in ADR-0007.

**Not reproduced: the trained OCR router and the neural quality classifier.** FinePDFs routes with an
XGBoost model trained on 1,350 hand-annotated PDFs and filters with ModernBERT/mmBERT heads. Those
artefacts are not available here and cannot be re-derived in this environment. The routing policy is
therefore an explicit, documented heuristic over the same *kind* of features (text density, coverage,
garbled ratio, image-only pages), behind a policy object so a trained model can replace it without
touching the pipeline. A quality classifier port is deliberately not introduced until there is a model
to put behind it (YAGNI). Recorded in ADR-0004.

**Language identification.** A port with a dependency-free adapter that always works. It names the
*script* (Latn, Grek, Cyrl) with a confidence equal to the winning script's share of letters, and reports
`unknown` rather than guessing below forty letters. It cannot tell English from German, and the README
says so. A fastText/GlotLID adapter replaces it behind the same port; the fastText one ships (see the
amendment below), and GlotLID does not.

*Amended during implementation (ticket 08).* Both the script adapter and the fastText adapter are now
shipped. `ScriptLanguageIdentifier` stays the default and the model-free fallback;
`FINEPDFS_LANGUAGE=fasttext` selects a fastText `lid.176` adapter, whose model is downloaded rather than
vendored (`FINEPDFS_LANGUAGE_MODEL`), whose library is an optional extra, and whose two-letter labels are
normalised to ISO 639-3 so `eng` means the same thing here as it does in GlotLID. The per-language
threshold table stays out: with one global confidence floor the decision is still readable, and a
per-language table is only worth its weight once there are enough languages in the corpus to tune it.
Recorded in ADR-0001, which owns the language seam.

## Testing Decisions

**What makes a good test here.** A test asserts observable behaviour through a port or through the
published output, using inputs and expected values that come from outside the implementation (a real
recorded WARC fragment, a hand-written expectation, a documented threshold). Tests must not mock
internal collaborators or assert on private helpers. If renaming an internal function breaks a test,
that test was wrong.

**Seams (three, all agreed up front).**

1. *Pipeline seam* — the application pipeline use case, driven with in-memory fakes for all five
   ports. This is the primary seam and carries most tests: routing, cleaning, language, publish and
   dedup behaviour, and the failure/exclusion path. It catches behaviour regressions and costs
   milliseconds. It does not catch adapter bugs or CocoIndex wiring bugs.
2. *Flow seam* — the CocoIndex application end to end over a small corpus of real PDFs (network
   marked): outputs exist, re-running reprocesses nothing, adding a file processes one, deleting a file
   removes its outputs. It catches wiring, memoization and target-state bugs. It does not isolate the
   cause of a failure.
The flow seam injects its runtime through the documented `configure_runtime` seam and does not patch an
internal function.

3. *Adapter contract seam* — the two pure protocol mappings (WARC record bytes to PDF payload; a VLM
   JSON response to pages) tested against recorded fixtures. It catches protocol drift without a live
   service. It does not test the network, retries or engine quality.

No test is written against internals. Where a helper is worth testing, it is a domain policy reached
through seam 1.

**Prior art.** This repository has no test suite; the tests established here are the prior art: pytest,
tests grouped by layer, fakes written by hand as small classes implementing the ports, a fixtures
directory holding one real Common Crawl WARC record fragment and one recorded VLM response, and one
network-marked end-to-end test.

## Out of Scope

- Training or acquiring the XGBoost OCR router or the ModernBERT/mmBERT quality classifier.
- Running PaddleOCR-VL or MinerU models inside CI, or downloading multi-gigabyte weights here.
- Full-crawl scale (476M documents), distributed execution, or datatrove parity (Slurm/Ray executors).
- PII anonymisation, per-language threshold tuning against an LLM judge, and pushing a Hugging Face
  dataset.
- Vector embeddings, a search index, or retrieval: this spec stops at a clean text corpus.
- Reproducing FinePDFs' exact numbers or claiming comparability with the published dataset.

## Further Notes

- This environment has no GPU. A branch and PR body can be prepared here, ready to be pushed and opened
  as a PR.
- Common Crawl PDFs are fetched live; the corpus directory is not committed.
- Engine licences differ (Docling MIT, PaddleOCR Apache-2.0, MinerU AGPL-3.0, pymupdf AGPL-3.0): the
  engine adapters are documented with their licence, and using an AGPL engine is the operator's choice.
