# CocoIndex PDF ingestion benchmark — methodology and results

> **Historical.** This benchmark measured a throwaway CocoIndex v1 prototype against 310 generated PDFs.
> It was superseded by `src/finepdfs/` (the pipeline that fetches real Common Crawl PDFs), and the
> synthetic corpus generator (`gen_pdfs.py`, `pdf_synth/`, `md_synth/`) was deleted at its owner's
> request — the numbers below were measured against it, so the "reproduce" lines for the synthetic
> corpus no longer apply. The real-paper corpus (`pdf_real/`) and its numbers still reproduce with the
> project virtualenv: `../.venv/bin/python run_bench.py full`.

Measured on this machine: 4 vCPU, 15 GB RAM, Ubuntu, glibc 2.39, Python 3.12.3.
Stack: cocoindex 1.0.25 (Rust engine bundled), pymupdf 1.28.2 (parser), fpdf2 2.8.9 (corpus generator).
What is measured: PDF bytes -> parse -> chunk (RecursiveSplitter, 2000/500, markdown) -> sha256-named
markdown artifact written via localfs target. No embeddings (no GPU on this box) — see notes.

## How to reproduce

~~~
cd /home/ubuntu/cocoindex/bench
./venv/bin/python gen_pdfs.py 300 8 pdf_synth     # synthetic corpus
./venv/bin/python fetch_real.py                   # 20 real arXiv papers
./venv/bin/python run_bench.py full               # cold/warm/edit/add/delete phases + baselines
./venv/bin/python run_bench.py smoke              # quick 20-doc sanity run
~~~

Files: ingest.py = the pipeline (memoized parse via @coco.fn.as_async(memo=True) + asyncio.to_thread,
memoized per-file component; sha256-named markdown artifacts). run_bench.py = phase driver.
NOTE: the driver mutates pdf_synth during phases (edit 20 / add 20 / delete 10) — regenerate for a clean rerun.
NOTE: fpdf2 auto page-breaks make the synthetic docs ~15 pages each (8 requested) — the corpus is
310 files x ~15 pages = 4,650 pages; real papers average ~25 pages each.

## Results

Corpus A (real): 20 arXiv papers, 55 MB, 502 pages.
Corpus B (synthetic): 310 files, 4,650 pages, ~20 KB/file.

| Phase | With memo=True on the component | Without memo (first run of the bench) |
|---|---|---|
| Cold, real-20 (parse+chunk+write) | 2.25 s -> 223 pages/s | 2.41 s |
| Cold, synth-310 | 7.36 s -> 632 pages/s, 42 docs/s, 1,097 chunks/s | 9.44 s (300 docs) |
| Warm rerun, zero changes | **0.30 s, unchanged=310, zero re-execution** | 2.56 s (re-split everything) |
| Edit 20 docs (content changed) | **0.88 s** (only those 20 re-parse) | 2.80 s |
| Add 20 new docs | **0.76 s** | 3.11 s |
| Delete 10 docs | **0.26 s** | 2.59 s |

Baselines (raw pymupdf parse only, no CocoIndex): 1,040 pages/s synthetic, 296 pages/s real.
So the full pipeline (engine + chunking + dedup + writing markdown artifacts) runs at ~60-75% of
bare parser throughput — engine overhead is real but modest, and parsing dominates.

## What the numbers prove (and what to watch)

1. **The delta contract holds exactly.** Warm rerun does zero work (0.30 s is source scanning +
   fingerprinting). Editing 20 of 310 docs re-parses only those 20 (300 of 4,650 pages).
   Deleting 10 docs costs 0.26 s and removes exactly 10 artifacts.
2. **memo=True on the per-file component matters a lot.** Without it, unchanged files re-run the
   cheap-but-nonzero part of the component on every catch-up (warm 2.56 s vs 0.30 s; edit 2.80 s
   vs 0.88 s here). Put memo=True on the per-item component, not just the expensive inner fn.
3. **sha256 artifact naming + target reconciliation = automatic dedup and garbage collection.**
   Editing a doc changes its sha, so the old artifact is dropped and the new one written — the
   artifact count stayed exactly right through every phase (no orphans accumulate).
4. **State grows like the memoized text.** LMDB state was 14 MB for 4,650 pages (~12 MB of markdown).
   Plan COCOINDEX_LMDB_MAP_SIZE at roughly the size of the text corpus you memoize.
5. **Embeddings were not measured here** (no GPU/torch on this box). In the config above the
   pipeline is parse-bound; adding a hosted or GPU embedder shifts the bottleneck to the embedder.
   Measure your embedder separately (chunks/s) and expect end-to-end throughput ~= min(parse, embed).
6. **Single-process extrapolation (this small VM):** 1M real-ish PDFs x 25 pages at ~220 pages/s
   is ~31 hours of cold backfill, one process, 4 cores. Partition the source across workers to
   scale out (CocoIndex state is per-process LMDB; do not share one store across machines).
   The payoff: after the backfill, daily catch-up only pays for the delta.

## Tuning knobs worth knowing

- asyncio.to_thread around the blocking parser (default executor ~8 threads here) — the engine
  parallelizes components; the thread wrap keeps the event loop free. For heavier parsers
  (docling), the repo's own example uses @coco.fn.as_async(runner=coco.GPU).
- batching=True on the embedder fn coalesces concurrent misses into one model call.
- COCOINDEX_LMDB_MAP_SIZE if the memo cache will be large (auto-grows otherwise).
- app.update() returns a handle exposing stats() — the per-phase engine counters
  (starts / unchanged / adds / deletes / reprocesses / errors) used above come from there.
