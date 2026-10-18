# PDF workflow demo runbook

A rehearsal of the P0 ingestion demo on real Common Crawl PDFs: routing, real Docling OCR, cleaning,
incremental reruns, duplicates, add/replace/remove and the two failure modes. Every number below was
measured on one host on 2026-10-18 against the pinned `CC-MAIN-2026-39` Corpus; nothing here is a
historical synthetic benchmark.

This runbook records commands and their answers. It does not assert that a particular Python, Docling
or CPU is installed on your machine: the version commands in [Environment](#environment) are how you
check, and the answers change without this file.

The Corpus itself is described in [`pdf-demo-corpus.md`](pdf-demo-corpus.md); the manifest is
[`pdf-demo-corpus-manifest.json`](pdf-demo-corpus-manifest.json). No PDF bytes, extracted text, model
weights or secrets are committed, and no host path is part of any command here.

## Isolated, persistent demo state

The demo command keeps one root for the corpus, the published output and the warm CocoIndex database,
so it can run beside a working checkout without sharing `corpus/`, `out/` or `cocoindex.db`:

```text
<root>/corpus/       restored PDFs + manifest.json
<root>/out/          declared outputs, including out/run-summary.json
<root>/state.db/     CocoIndex database (memo table + target state)
```

`<root>` defaults to `.demo/` in the repository root and is gitignored. A run never deletes it; only
the explicit `reset` subcommand removes those three paths (and nothing else under the root). That is
what lets a second run reuse the first run's memo instead of paying for OCR again.

## Commands

Run from the repository root. The module form is used because the console script's shebang names the
interpreter it was built against (`head -1 .venv/bin/finepdfs`); an installed package also provides
`finepdfs-demo` with the same arguments.

```bash
# 1. restore the pinned Corpus once (network; ~27 MB of ranged WARC reads)
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo restore

# 2. first run: genuinely cold, with the real Docling engine for the two scans
#    --search declares out/chunks/, which is what the search stages below index
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo run --search \
  --engine docling --engine-command docling

# 3. unchanged rerun: same command, same state; must reuse all 18 Extractions
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo run --search \
  --engine docling --engine-command docling

# 4. cheap policy change: a new publish floor, no new Extraction
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo run --search \
  --engine docling --engine-command docling --min-characters 5000

# 5. what the demo holds and what the last run declared (runs nothing)
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo status

# 6. only when a genuinely cold start is wanted: delete corpus, output and state
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo reset
```

Use `--root <dir>` on every command to rehearse in another directory. The commands above are the
whole workflow; `restore` is the only step that reaches the network.

Expected restore output. `demo restore` and `finepdfs restore-corpus` now dispatch to one
implementation (`src/finepdfs/interfaces/restore.py`), so there is no second replay to disagree with
this text (recorded in [`pdf-demo-corpus.md`](pdf-demo-corpus.md)):

```text
restoring 19 pinned PDFs from CC-MAIN-2026-39 into <root>/corpus
records seen 19, fetched 18, already present 1, failed 0
```

The second time, every record is already present: `records seen 19, fetched 0, already present 19,
failed 0`.

## Environment

Check the tool versions; these are the answers on the rehearsal host:

| Check | Observed |
| --- | --- |
| `.venv/bin/python --version` | Python 3.12.3 |
| `.venv/bin/python -m pip show cocoindex pymupdf` | CocoIndex 1.0.25, PyMuPDF 1.28.2 |
| `docling --version` | Docling 2.140.0 (Docling Core 2.101.1, IBM Models 4.0.3, Parse 7.22.3) |
| `python -c "import torch; print(torch.__version__)"` | 2.10.0+cpu, `torch.cuda.is_available()` false |
| `python -c "import easyocr; print(easyocr.__version__)"` | 1.7.2 |
| `nproc`, `lscpu`, `free -g` | 4 vCPU (AMD EPYC 9354P), 15 GiB RAM, no GPU |

Check the install with the documented commands rather than assuming it. The base install is pinned
exactly (`cocoindex==1.0.25`, `pymupdf==1.28.2`) and the OCR tooling this rehearsal used is recorded in
[`requirements-demo.txt`](requirements-demo.txt), so a fresh install reproduces the same page text,
chunk ids and citations:

```bash
.venv/bin/pip install -e ".[docling]"        # pinned: docling==2.140.0, never a base dependency
.venv/bin/pip install torch==2.10.0+cpu --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r docs/demo/requirements-demo.txt
.venv/bin/python -c "import torch, easyocr; print(torch.__version__, torch.cuda.is_available(), easyocr.__version__)"
```

Docling was installed in an isolated CPU virtualenv, so the repository environment is untouched; the
engine command is a local CLI (`--engine-command docling`), and its Engine Identity is
`docling@cli:docling:v2.140.0` -- the basename plus the probed version, not the path. The EasyOCR and
Docling layout weights are downloaded on first use, not committed. The rehearsal used CPU torch; the
only live engine validated here is Docling.

## Corpus size

`CC-MAIN-2026-39`, 19 Crawl Records covering **18 distinct Documents, 325 pages and 27,116,937 bytes**
of PDF. One pair of records is the same digest at two real `buoybay.noaa.gov` URLs, which is what exact
duplication means.

What this Corpus does and does not demonstrate is stated in
[`pdf-demo-corpus.md#what-this-does-and-does-not-demonstrate`](pdf-demo-corpus.md#what-this-does-and-does-not-demonstrate).
In short: it demonstrates routing, real OCR, cleaning, duplicates, exclusions and incrementality on a
small fixed input. It does **not** demonstrate retrieval quality or ranking at corpus scale; 18
Documents is not a corpus-scale claim. Search, the browser's search view and the agent tools are
covered below as the budget-driven local fallback over the same accepted records.

## Measured runs

The first three rows are the recorded rehearsal logs (Docling for the two scans, 18 Extractions).
"Elapsed" is the observed wall-clock time of the run and "wall" is `time` around the whole command;
at rehearsal time the observed elapsed was read from a field in `out/run-summary.json`, and this branch
moves that observation out of the declared file into `out/last-run.json` (see
[the run summary and the observed log](#the-run-summary-and-the-observed-log)). The numbers were
measured then and are not re-measured here. "Calls" are actual engine calls (memo misses), "reused"
are memo keys the reading pass asked for and served from CocoIndex's memo.

| Run | What changed | Calls | Reused | Routing | Published | Exclusions | Observed elapsed | Wall |
| --- | --- | ---: | ---: | --- | --- | --- | ---: | ---: |
| cold | nothing (empty `state.db`) | 18 | 0 | native 16, ocr 2 | 17 docs / 313 pages | 2 (exact 1, near 1) | 133.8 s | 134.2 s |
| unchanged | nothing | 0 | 18 | native 16, ocr 2 | 17 docs / 313 pages | 2 (exact 1, near 1) | 8.6 s | 9.0 s |
| policy | `--min-characters 5000` | 0 | 18 | native 16, ocr 2 | 14 docs / 308 pages | 5 (dropped 3, exact 1, near 1) | 8.9 s | 9.3 s |

The cold run is dominated by the two genuine scans: Docling took 54.5 s on the one-page scan and
73.0 s on the two-page scan, and the extract body is synchronous, so those calls serialise. The
`pymupdf` Documents are effectively free. The two cheap runs differ by seconds, both spent in the
deciding pass and target-state apply, not in an engine.

### The demo command itself

The command was exercised in both directions on the same host and Corpus. A genuinely cold run on a
copied corpus with no state database took **148.6 s wall / 148.2 s observed elapsed, 18 calls,
0 reused, 17 published, 313 page records, 2 exclusions** -- the same counters as the CLI cold run
above, because it is the same CocoIndex flow; only the directory and environment handling belong to
the command.

A run on a warm copy (state database restored alongside the corpus, so no Extraction was needed)
measured the command's own overhead: **8.7 s wall / 8.2 s observed elapsed, 0 calls, 18 reused,
17 published, 313 page records, 2 exclusions**. A second unchanged run took **8.5 s wall / 8.2 s
observed elapsed** and rewrote every output byte-for-byte except `out/run-summary.json`, whose
timestamps changed on that revision. That was the visible breach of "a published record must be
reproducible": the declared file carried when the run happened, so no two runs could declare the same
bytes. This branch removes the timing from the declared summary, so the same rerun now rewrites
identical `run-summary.json` bytes and the observation lands in the non-declared `out/last-run.json`;
`tests/infrastructure/test_run_summary.py` and `tests/interfaces/test_cli.py` pin that. This is
evidence that the demo command does not wipe the warm state: the second run did no engine work at all.
The same warm root with `--min-characters 5000` reported **8.7 s wall / 8.3 s observed elapsed,
0 calls, 18 reused, 14 published, 308 page records and 5 exclusions**, so the policy change above also
holds through the command.

## The run summary and the observed log

Each run declares `out/run-summary.json` as target state -- the flow is the only writer. `finepdfs
run` and `finepdfs-demo run` print a one-line view of that same file; `finepdfs-demo status` prints
the last one. Nothing recomputes a counter from the output directory.

The declared file is **reproducible**: it carries the counters of the corpus, policy and state it ran
over, and nothing that changes when nothing changed. A rerun over the same state rewrites the same
bytes. No timestamp, duration, host path or lease id is in it, which is what
`CODING_STANDARDS.md`'s "a published record must be reproducible" and ADR-0006's "run again: no
changes" require.

Schema version 1:

```json
{
  "schema_version": 1,
  "corpus": {"crawl_id": "CC-MAIN-2026-39", "domain": "…", "crawl_records": 19, "documents": 18},
  "routing": {"documents": 18, "native": 16, "ocr": 2},
  "extraction": {"requested": 18, "calls": 0, "reused": 18},
  "published": {"documents": 14, "page_records": 308},
  "exclusions": {"total": 5, "by_kind": {"dropped": 3, "exact-duplicate": 1, "failed": 0, "near-duplicate": 1}},
  "engine": {"name": "docling", "identity": "docling@cli:docling:v2.140.0"}
}
```

Timing lives beside it in `out/last-run.json`, written by the command that ran the pipeline (never by
the flow, so it is not declared target state and target-state apply neither writes nor retracts it):

```json
{
  "observed": true,
  "note": "observed wall-clock timing of one run; it is not declared target state and is not reproducible",
  "started_at": "…Z",
  "finished_at": "…Z",
  "elapsed_seconds": 8.311
}
```

A run that the CLI or the demo did not wrap (for example `cocoindex update main.py` by hand) leaves no
such file, and the browser then shows elapsed as **not recorded** rather than zero. The file is
labelled observed everywhere it is read or printed; the terminal line appends `elapsed 8.3s (observed)`
only when one was recorded.

Two things to read carefully:

* `extraction.calls` is counted inside the memoised extract body, so it is one actual engine call and
  never a memo hit. `extraction.reused` is `requested - calls` over memo keys, i.e. observed reuse of
  the run's own work, not a CocoIndex component statistic. The two are deliberately different numbers
  from CocoIndex's "unchanged" line in the progress output.
* `elapsed_seconds` is in the observed log, not the summary, and measures the command around the run,
  including CocoIndex's target-state apply. `routing.documents` counts routed Documents, so the
  exact-duplicate record that is skipped before routing is not in it; `corpus.documents` is the
  distinct-digest count.

`exclusions.by_kind` always carries all four kinds (`dropped`, `failed`, `exact-duplicate`,
`near-duplicate`) with zeros, so a reader can depend on the shape. The terminal line prints only the
non-zero kinds.

An exact duplicate only names a keeper that is actually published. If the digest keeper is itself a
near-duplicate loser, its exact duplicates follow the keeper's near decision and name the published near
keeper, with the same similarity; if the keeper was dropped or failed, the duplicate is declared with
that same honest disposition and a reason naming the keeper, and no exact row is written. None of this
adds an Extraction: the decision is made from decisions, never from more reading.

## Routing and the real OCR page evidence

Of 18 Documents, **16 route to the native text layer and 2 to Docling**: the two genuine image-only
AOML dropsonde scans. Their declared page exports are the inspected evidence (from a default-publish
run; the `--min-characters 5000` run above drops both by character count, so their page files are
retracted and only the summary counters remain).

```bash
grep -l Dropwindsonde out/pages/*.jsonl
# out/pages/5861c9f211cace9b.jsonl   (one-page scan)
# out/pages/0ae09935a21c39d4.jsonl   (two-page scan)
```

`5861c9f211cace9b` -- `20090827I1droplog.pdf`, one page:

* `page_number` 1, 342 characters, `engine` `docling@cli:docling:v2.140.0`.
* Text begins `storm` / `Flight ID`, then the printed form header
  `GPS Dropwindsonde Scientist Log (Revised 5/2002)` and the table header
  `Drop | ID | Tine (UIC ) | Lat | Ion | Surface Pressure | …`.

`0ae09935a21c39d4` -- `20090826I2droplog.pdf`, two pages, mapped 1 and 2:

* page 1: 1762 characters, begins `Flight ID` / `Dropsonde Scientist` and the lead-project-scientist
  narrative; it does **not** contain the form header.
* page 2: 414 characters, contains `GPS Dropwindsonde Scientist Log (Revised 5/2002)` and the table
  header. This is the page the printed header actually belongs to, so the two pages are distinct and
  no page was collapsed onto page 1.

The page numbers are the tool's original 1-based `prov[].page_no` values, and each page export is one
JSON object per accepted page carrying `doc_id`, `source_url`, `digest`, `page_number`, `text` and
`engine`.

**Handwritten content is not usable.** The same exports show it plainly: `12 | Jicok | 61`,
`WxwxA Dannyl`, `Presgure`, `Comantg`. Printed headers, labels and the narrative body are readable;
sonde IDs, times, coordinates, pressures and comments mostly are not. This is a limitation of the
run, not of the page mapping, and the runbook does not present the text as a usable transcription of
the handwritten log.

The Tesseract-CLI path is not usable on this one-page scan either: with `--ocr-engine tesseract`,
Docling 2.140.0 runs its OSD step unconditionally and rotated the page 180 degrees from a low-confidence
reading, producing character-reversed text. That was measured and diagnosed, not assumed, and the
default Docling (EasyOCR) path is what the numbers above use.

## Duplicates, and what the exclusions say

Exact duplicate (real, in the pin): two records, one digest, keeper the smallest URL
`https://content.buoybay.noaa.gov/.../s_geography_text.pdf`. The other record is declared:

```json
{"url": "https://www.buoybay.noaa.gov/.../s_geography_text.pdf",
 "kind": "exact-duplicate",
 "reason": "identical bytes to https://content.buoybay.noaa.gov/.../s_geography_text.pdf"}
```

Near duplicate (real, in the pin): the two Winter 2023-24 seasonal summaries, similarity **0.969** on
the accepted, cleaned text. The April capture is keeper; the May capture is declared:

```json
{"url": "https://buoybay.noaa.gov/.../2024-05/NCBOWinter202324SeasonalSummaryA.pdf",
 "kind": "near-duplicate",
 "reason": "near-duplicate of https://buoybay.noaa.gov/.../2024-04/NCBOWinter202324SeasonalSummary.pdf (similarity 0.969)"}
```

A Document can be both: its bytes are identical to another Document's, but that digest keeper is
itself a near-duplicate loser. The two kinds then conflict, because the honest row is `near-duplicate`
naming the published near keeper -- not `exact-duplicate` pointing at a Document the run did not
publish. Honesty wins: `domain/dedup.decide_duplicates` reclassifies the row and never invents a kind
to keep a label that would be false, and `tests/infrastructure/test_flow_exact_duplicates.py` pins it.
The same decision feeds `dry-run`, so the estimate cannot disagree with the declaration.

Bad input: the pinned Corpus contains no bad Document (the truncated captures in this crawl slice
cannot be restored), so this was rehearsed with the controlled `tests/fixtures/not_a_pdf.pdf` added
to a copy of the Corpus and labelled controlled. One Document became a `failed` exclusion with the
reason `these bytes are not a readable PDF: Failed to open stream`; the other 17 still published.
The run exits 0 in that case because something was published -- the exclusion, not the exit code, is
the signal -- and exits 1 only when nothing at all is published.

## Add, replace and remove

All of these ran against a warm state (18 Extractions already memoised), so only genuinely new bytes
pay for an engine call:

| Rehearsal | Records / Documents | Calls | Reused | Published | What it shows |
| --- | --- | ---: | ---: | --- | --- |
| add one Document | 20 / 19 | 1 | 18 | 18 docs / 315 pages | only the new Document is read |
| replace another Document's bytes | 20 / 19 | 1 | 18 | 18 docs / 233 pages | the new digest is read; obsolete pages are retracted |
| remove a record | 19 / 18 | 0 | 18 | 17 docs / 208 pages | the removed Document's declared outputs are deleted |
| remove the exact keeper | 18 / 18 | 0 | 18 | 17 docs / 208 pages | the surviving record is promoted and published; no exact exclusion remains |
| remove the near keeper | 17 / 17 | 0 | 17 | 17 docs / 208 pages | the May capture is published, similarity no longer applies |

Replacement is the one that changes page count. The replaced Document is the 84-page
`Facilitators-Guide-to-MWEE-Training-2023.pdf`; its replacement capture is 2 pages, so the target
state retracts the 82 pages that no longer exist (315 -> 233 total). Removing a record, by contrast,
deletes that 25-page Document's pages and leaves the rest untouched (233 -> 208).

Keeper promotion is deterministic, not incidental: the keeper is the smallest URL, so when the keeper
leaves the manifest the surviving record becomes the keeper of that digest and is published under its
own key, with no duplicate exclusion pointing at a record that is gone.

### Failing loudly, without inventing a deletion

* **Digest mismatch** (bytes written into a digest-named file do not hash to the name): the run logs
  `<path> does not hold the bytes its digest names`, records that Document as `failed`, retracts its
  outputs, and publishes the other 16. No stale text is served for the changed bytes.
* **Missing bytes** (a declared PDF removed from disk, manifest unchanged): logged as
  `[Errno 2] No such file or directory`, again a `failed` exclusion. The record is still declared, so
  this is not treated as an intentional source deletion and the next run retries it when the bytes are
  back; the *manifest* is the only thing that removes a Document's outputs.
* **Unavailable remote URL** is the same case at restore time: `restore` writes the pinned entries to
  the manifest before fetching, prints `failed: <url> (<reason>)` for a record it could not read, and
  exits 1 (recorded in [`pdf-demo-corpus.md`](pdf-demo-corpus.md#failure-behavior)). The record stays
  declared, so a later run records a failed Exclusion rather than mistaking unavailability for a
  source deletion.

In every case the summary shows the failure: `failed` in `exclusions.by_kind` and one fewer
`routing.documents` than `corpus.documents`.

## Search accepted evidence: the local fallback

The source spec proposed a pinned single-node Elasticsearch for this stage and allowed exactly one
cut: if it cannot be made stable inside the budget, index the *same record contract* in a local
full-text store and ship one backend, never two. The check is `command -v elasticsearch` plus the
configured endpoint; on the rehearsal host (2026-10-18) neither existed, and standing one up reliably
did not fit the remaining budget. So the one shipped backend is **SQLite FTS5** (Python standard
library), behind the shared
contract, with **no embeddings**. This is the budget-driven fallback, not a second permanent design;
if a real deployment is ever wanted it replaces this adapter rather than sitting beside it (see
[ADR-0008](../adr/0008-one-local-search-backend.md)).

### What the flow declares

Every published Document already declared its accepted Page Texts under `out/pages/<key>.jsonl`. When a
run enables search (`--search`, or `FINEPDFS_SEARCH=1`) it also declares deterministic chunks under
`out/chunks/<key>.jsonl`; a plain curation run declares no chunk export and retracts a stale one, per
the spec's "chunks when search is enabled". One JSON object per line:

```json
{"chunk_id": "0a1b2c3d4e5f60718293a4b5c6d7e8f9", "chunking_version": "chunking-v1",
 "digest": "…", "doc_id": "5861c9f211cace9b", "end_offset": 342, "engine": "pymupdf@…",
 "language": "Latn", "ordinal": 0, "page_number": 1, "source_url": "https://…",
 "start_offset": 0, "text": "…"}
```

Chunking rules (`chunking-v1`; bumping `CHUNKING_VERSION` changes every `chunk_id`):

* only accepted Page Texts are chunked, under the original 1-based page number;
* a chunk never spans pages, so a citation is one page;
* a page is split at blank lines and paragraphs are grouped up to 1,200 characters; an over-long
  paragraph is split at whitespace;
* `start_offset`/`end_offset` are character offsets into that page's exported text, and
  `text == page_text[start_offset:end_offset]`;
* `ordinal` runs over the Document's chunks in page order.

Pages and Markdown are unchanged by this, and they are declared with or without `--search`. Because
chunks are declared target state like the pages, a replacement with fewer pages, a Document that becomes
an exclusion or duplicate, or a Crawl Record that leaves the manifest retracts the obsolete chunk files
on the next run -- the index never has to guess. Turning search off retracts them all.

### Commands

```bash
# rebuild the index from the current declared chunks (idempotent; deletes rows no longer declared)
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli index-search \
  --out .demo/out --index .demo/out/search.db

# query it; metadata filters and JSON output use the same contract the browser and agent read
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli search \
  --out .demo/out --index .demo/out/search.db "dropwindsonde"

PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli search \
  --out .demo/out --index .demo/out/search.db --language Latn --engine pymupdf@… --top-k 5 --json "storm"
```

`index-search` prints how many chunks were stored and how many were retracted, and exits 1 if any
chunk failed to store (each failure names the `chunk_id` and the reason). `search` exits 2 when the
index is missing, empty or stale-- it never prints a hit it cannot vouch for -- and prints no matches
rather than an error when a valid query finds nothing.

One resolver (`src/finepdfs/interfaces/search_paths.py`) gives `index-search`, `search`, `explore` and
`ask` the same index path: an explicit `--index`, else `FINEPDFS_SEARCH_INDEX`, else `<out>/search.db`.
The same file resolves `<out>`: an explicit `--out`, else `FINEPDFS_OUT`, else `out/`. `search`,
`explore` and `ask` need that directory because it holds the chunks, pages and index they compare
against; before this helper `ask` ignored `FINEPDFS_SEARCH_INDEX`, `index-search` preferred `--out`
over it, and `explore`/`ask` hard-coded `out/`, so `FINEPDFS_OUT` never named what they served.

The index is derived state, not declared by the flow, and it now records a **fingerprint of the
declared chunk set it was built from**. Every read compares that fingerprint with the current declared
set:

* a replacement, a removal, an exclusion, a chunking-version bump or a run with search turned off makes
  the index stale, and the answer is `available: false` with the reason and "run `index-search`" -- no
  hits, and no stale text;
* a missing index, an empty one and a 0-byte one are the same kind of answer;
* a partial rebuild (a chunk that failed to store) deliberately leaves the index stale until a clean
  rebuild, so a half-built index is never presented as current.

Reading is strictly read-only: SQLite is opened with `mode=ro`, no schema is created and nothing is
written, so serving the explorer cannot rebuild a database or become a second writer. Only
`index-search` writes. `tests/infrastructure/test_search_index.py` and
`tests/interfaces/test_explorer.py` pin the stale answers and that a 0-byte `search.db` stays 0 bytes.

### The contract

`finepdfs/domain/search.py` is the shape; no backend name appears in it. A hit carries `doc_id`,
`chunk_id`, source URL, PDF digest, original page number, chunk offsets/ordinal, Engine Identity,
language, `text`, a highlighted `snippet` (`<mark>…</mark>`) and a BM25 `score` (larger is better; only
the ordering is meaningful). `finepdfs/application/search.py` exposes `SearchAcceptedEvidence.search(...)`
for the browser and `search_pdfs(index, declared, query, filters, top_k)` for the agent; both validate a
non-empty query, bound `top_k` to 100, accept the same `SearchFilters(language, engine, doc_id)` and
refuse to answer from an index that does not mirror the current declared chunks, so the explorer and
the agent cannot return stale hits either.
The SQLite adapter lives in `finepdfs/infrastructure/search/sqlite_fts.py` and satisfies the
`SearchIndex` port. `interfaces/explorer.py` reads this contract for the browser: `GET /api/search`
takes `query`, optional `language`, `engine` and `doc_id` filters, and `top_k`, and returns the same
`available`/`query`/`filters`/`total`/`hits` shape. The page has a search box and a results region; an
absent, empty, schema-less or stale index is an explicit
`{"available": false, "reason": "... run finepdfs index-search ..."}` with no hits, and the view never
builds, migrates or repairs the index it reads.

```bash
# the same read through the browser view; --index defaults to <out>/search.db
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli explore \
  --out .demo/out --corpus .demo/corpus --index .demo/out/search.db
# then open http://127.0.0.1:8765/ and search accepted evidence
```

The explorer is still read-only: searching opens the existing index and writes nothing. The page renders
the highlighted snippet by building text nodes around `<mark>`, so extracted text is never injected as
HTML.

### Add, replace and remove, seen through search

* **Add:** run the pipeline, then `index-search`; the new Document's chunks are returned with its own
  `doc_id`, digest and pages.
* **Retry:** re-running `index-search` upserts by `chunk_id` and retracts nothing, so the same evidence
  is not returned twice.
* **Replace / fewer chunks:** the flow rewrites the Document's chunk file; `index-search` retracts every
  old `chunk_id` and no hit names the old digest or a page that no longer exists.
* **Chunking-version change:** bumping `CHUNKING_VERSION` produces new ids; the old ids are retracted,
  so the same sentence is not found twice under two versions.
* **Exclusion / removal:** a dropped, failed, duplicated or removed Document declares no chunk file, so
  its rows are deleted and it cannot supply evidence.

### Limitations

* **One host, lexical only.** SQLite FTS5 gives BM25, filters and snippets; it does not give
  Elasticsearch's analysis chains, scale-out or score comparability, and there are no embeddings or
  reranking. This fallback is allowed by the spec for this demo, not a claim of parity.
* **No retrieval-quality claim.** 18 Documents cannot demonstrate ranking quality at corpus scale; the
  tests prove freshness, citations and the absence of stale duplicates, not relevance.
* **Query terms are OR'd** (each quoted as a literal) so a multi-word question still returns evidence;
  BM25 ranks chunks matching more terms higher. Punctuation is not FTS5 syntax, so it cannot break a
  query.
* **The index is derived and local.** It must be rebuilt after a run; until then it reports itself
  stale instead of answering. Deleting it costs one `index-search`, no Extraction. Every read
  re-derives the freshness fingerprint from the declared chunk files -- a directory read over a
  few hundred small JSONL files for this Corpus, not a rebuild.
* **Page reads are the agent's second tool.** `read_pdf_page(doc_id, page_number)` reads the declared
  page export, not Markdown and not the index; it refuses an absent, excluded, duplicate, unpublished,
  retracted or stale page explicitly. See [Agent tools](#agent-tools-search-and-inspect-accepted-pages).

## Agent tools: search and inspect accepted pages

The agent stage is a small bounded loop over the same accepted evidence, not a second writer and not a
second search backend. Two tools are exposed to an OpenAI-compatible chat model:

* `search_pdfs(query, filters, top_k)` reads the shared search contract and returns ranked current hits
  with `doc_id`, `chunk_id`, source URL, PDF digest, original page number, snippet/text and score.
* `read_pdf_page(doc_id, page_number)` returns the exact accepted Page Text of one original page. An
  absent, excluded, duplicate, unpublished, retracted or stale page is an explicit
  `status: not-found`/`unsupported` result with a reason, never text reconstructed from Markdown.

Both are application functions (`finepdfs/application/search.py` and
`finepdfs/application/page_reads.py`), so the CLI, the loop and the tests share one contract. The loop
(`finepdfs/application/agent.py`) caps tool executions at six by default and prints every call and
result, and its citation contract is enforced rather than requested:

* a final answer may cite only a page `read_pdf_page` returned as accepted; a citation for any other
  page is removed from the answer text, and a search snippet never makes a page citable;
* a page number in prose ("page 37") is not a citation;
* when the answer has no verified citation, or carries one for a page nobody read, the loop takes one
  bounded repair turn asking the model to read the supporting page; if that does not produce a verified
  citation, the answer becomes `INSUFFICIENT EVIDENCE`;
* a contradictory `INSUFFICIENT EVIDENCE` tail after a supported answer is dropped, and an
  insufficient-evidence reply carries no citations.

Every citation the CLI prints therefore names a page that was read, and `--json` still carries the
`verified` flag and the full tool trace. When the tool budget is spent, one final completion without
tools is allowed to force an answer or an explicit insufficient-evidence reply; that answer goes through
the same contract, so an unverified claim is reported as insufficient rather than cited.

### Commands

```bash
# ask over the current declared evidence; the index must exist first
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli ask \
  --question "What rate of wind-speed increase over 24 hours counts as rapid intensification?" \
  --base-url 'http://[::1]:11434/v1' --model deepseek-v4.1-flash:cloud \
  --out .demo/out --index .demo/out/search.db --max-tool-calls 6

# the same answer, citations and full tool trace as JSON
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli ask \
  --question "..." --base-url 'http://[::1]:11434/v1' --model deepseek-v4.1-flash:cloud \
  --out .demo/out --index .demo/out/search.db --json
```

`ask` exits 2 when the index is absent (run `index-search` first) or a flag is invalid, and 3 when the
chat endpoint cannot answer. The API key comes from `FINEPDFS_ASK_API_KEY` by default, or `--api-key`;
it is sent as a bearer header and never printed. `--top-k` sets the hits a search call returns when the
model does not ask for a count.

### Model capability, checked rather than assumed

The rehearsal host serves several models from Ollama at `http://[::1]:11434` (IPv6; the IPv4 address
timed out). A model list is not evidence of tool support, so the check was an actual inference with a
tool schema: `deepseek-v4.1-flash:cloud` returned real `tool_calls` with schema-valid arguments in
**0.49 s**, and `deepseek-v4-pro:cloud` in 0.68 s. The cloud-tagged model was used for the question
runs and produced real tool calls on every question, so the larger model was not needed.
`qwen3:1.7b` is still served and also returns real tool calls, but it missed three of the six questions
(an unread page cited on Q1, a contradictory `INSUFFICIENT EVIDENCE` tail on Q3, a prose-only "page 37"
on Q4); the contract now strips an unread-page citation, drops the contradictory tail and refuses a
prose page number, and the model difference is recorded rather than hidden.

The five answerable questions and one unanswerable control are in
[`agent-questions.md`](agent-questions.md); the observed answers, citations, tool traces, timings and
limitations are in [`agent-evidence.md`](agent-evidence.md). Summary of the two `deepseek-v4.1-flash:cloud`
runs, both with the enforced contract:

| # | Result | Citation | Verified page read? |
| --- | --- | --- | --- |
| 1 | correct, citing the page that states it | `9bf665589f7333a8 p.8` | yes |
| 2 | correct | `6dd127bebc737740 p.5` | yes |
| 3 | correct, no contradictory tail | `24564fdbfbb89e55 p.3` | yes |
| 4 | correct, structured citation instead of prose | `0adb664b1fafd254 p.37` | yes |
| 5 | correct | `8cc4c231717ef28f p.33` | yes |
| 6 | `INSUFFICIENT EVIDENCE`, no citation | none | n/a |

The agent evidence state is the text-layer run (15 published Documents / 310 page records / 590
chunks), so those answers are not OCR evidence; the two scans were dropped. The questions avoid them.
No retrieval-quality claim is made from 15 Documents. What is verified is that the tools are called,
their results round-trip, the budget holds, every citation names a page the tools read, and an
unsupported answer is reported as insufficient evidence; answer correctness beyond that is a model
property, observed twice per question here.


## The browser explorer, verified

The P1 explorer was exercised in a real browser on 2026-10-18 against a completed current-head
output; the "no browser" wording this file used to carry was wrong. Chrome was Google Chrome
148.0.7778.96 driven by `chrome-devtools-axi` 0.1.44. Serve a completed run and open the page:

```bash
# one completed current-head run with search chunks; pymupdf was the only engine installed
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo run --root <root> --engine pymupdf --search
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli index-search --out <root>/out
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli explore \
  --out <root>/out --corpus <root>/corpus --host 127.0.0.1 --port 8791
# then open http://127.0.0.1:8791/
```

Observed:

* **Status table:** 19 rows for 19 declared Crawl Records; the summary read 19 / 18 documents,
  routing native 16 / ocr 2, extraction calls 0 / reused 18, 15 documents / 310 page records,
  exclusions 4 (dropped 2, exact-duplicate 1, near-duplicate 1), elapsed 1.4 s (observed), engine
  `pymupdf@1.28.2`.
* **Filters:** `storm` in URL-or-text -> 6 rows; Routing `text-layer` -> 15; Status
  near-duplicate 1, exact-duplicate 1, dropped 2, published 15.
* **Document detail:** source URL, `Latn`, Engine Identity, routing reason, "44 accepted, 1
  dropped, 45 original", and accepted pages listed as Page 2...45 in original numbering with one
  `Open PDF page N` link each.
* **Retained PDF:** a page link opened `/pdf/<digest>.pdf#page=2` and Chrome's viewer reported
  `2 / 45`, so the locally retained PDF is served at the original page.
* **Refresh:** an unchanged warm rerun (0 calls, 18 reused, same counters) moved the observed
  elapsed from 1.5 s to 1.4 s and the finish timestamp; the page's poll and `Refresh now` both
  showed the new observation while the declared counters stayed identical.
* **Search highlight:** `storm` returned 5 hits over the 590 current chunks, and the snippets held
  5 real `<mark>` elements (each `storm`, computed background
  `color(srgb 0.541176 0.294118 0 / 0.35)`), not plain text.
* **Refresh after a completed update:** with the 15-Document / 590-chunk run indexed, a stricter
  policy (`--min-characters 32000`, still 0 calls / 18 reused) removed the Documents behind all
  5 `storm` hits; the page's poll, then `Refresh now`, each re-asked the active query, cleared the
  hits and showed the API's "the search index is stale: ... run finepdfs index-search" state.
  After `index-search` rebuilt that run's 347 chunks (243 retracted), refresh returned 1 hit and
  1 `<mark>`; restoring the default policy and rebuilding the 590-chunk index returned 5 hits and
  5 `<mark>` from the current chunks.
* **Console:** no messages, so no console errors; every request answered 200.

Screenshots were temporary evidence under `/tmp/pdf-demo-greptile-b/`
(`01-search-highlighted.png`, `02-refresh-stale-search-cleared.png`,
`03-refresh-rerun-highlight.png`; the earlier table/detail/PDF/filter captures are under
`/tmp/pdf-demo-browser-verify/`) and are not committed. Limitations: the verification host had no
Docling, so the two OCR-routed scans produced no text and were dropped by the publish floor -- the
routing filter therefore offered only `text-layer`, and an `ocr` routing reason and a Docling Engine
Identity were not re-verified here. The stale-hit refresh used a publish-policy change; a removed
Crawl Record reaches the same declared-chunk freshness check but was not repeated in the browser.
Search here shows the citation and freshness architecture of 15 Documents, not retrieval quality at
corpus scale.


## Dry run is not a free estimate

`finepdfs dry-run` curates in process through `CurateDocument`: it does not go through the CocoIndex
flow, so the extraction memo does not apply. Every dry run can call the real OCR engine for every
Document, every time, including the two scans. It writes nothing and leases a GPU itself when one is
configured, but it is **not** a cheap probe and its cost is not a lower bound on a warm rerun. Use
`run` for incrementality; use `dry-run` only when you want the curate path exercised on the current
corpus.

## Failures and limitations

* **Handwritten OCR is mostly unusable.** Only printed headers, labels and the printed narrative are
  reliably recovered. Handwritten table fields are noise. Do not cite this run as handwriting support.
* **The Tesseract-CLI Docling path is not usable here** (180-degree OSD rotation on the one-page
  scan). The default EasyOCR path is the one measured.
* **No real mixed-language Document.** No Document with pages in two natural languages was found in
  this crawl slice. `tests/fixtures/mixed_language.pdf` is a controlled English/Spanish/Russian
  fixture and is not extraction-quality evidence. Under the default `script` Language Mode the real
  Spanish newsletter is `Latn` on every page, the same tag as English, so this Corpus does not
  demonstrate per-language tagging either.
* **No real bad input in the pin.** The `not_a_pdf.pdf` rehearsal is controlled, and labelled so.
* **No retrieval claim.** 18 Documents, one host, one engine; search here is the local SQLite FTS5
  fallback described above. The demo demonstrates the ingestion and citation architecture, not
  retrieval quality at corpus scale.
* **One CPU host, one engine.** Only Docling was run live. PaddleOCR-VL and MinerU are tested against
  recorded shapes, not live weights. The cold timing is host- and model-specific and is not a
  benchmark; the 54.5 s / 73.0 s per-scan figures are single warm-cache observations.
* **The warm cache is disposable, not magic.** Deleting `state.db` costs one re-read of every
  Document; changing the flow's component shape can invalidate declarations, and the safe move is a
  `reset` (or deleting the database) and one cold run. A Docling version change changes the Engine
  Identity and re-reads the scans by design (ADR-0005).
* **Restore needs the crawl to still serve the pinned ranges.** If a WARC object disappears,
  `restore` fails visibly for that record; keeping a local copy of the corpus is how a rehearsal stays
  independent of that.
