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
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo run \
  --engine docling --engine-command docling

# 3. unchanged rerun: same command, same state; must reuse all 18 Extractions
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo run \
  --engine docling --engine-command docling

# 4. cheap policy change: a new publish floor, no new Extraction
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo run \
  --engine docling --engine-command docling --min-characters 5000

# 5. what the demo holds and what the last run declared (runs nothing)
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo status

# 6. only when a genuinely cold start is wanted: delete corpus, output and state
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo reset
```

Use `--root <dir>` on every command to rehearse in another directory. The commands above are the
whole workflow; `restore` is the only step that reaches the network.

Expected restore output (the same replay `finepdfs restore-corpus` performs, recorded in
[`pdf-demo-corpus.md`](pdf-demo-corpus.md)):

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
Documents is not a corpus-scale claim, and the browser/search stages are outside this runbook.

## Measured runs

The first three rows are the recorded rehearsal logs (Docling for the two scans, 18 Extractions);
`elapsed` is the run summary's own measure, wall time is `time` around the command. "Calls" are
actual engine calls (memo misses), "reused" are memo keys the reading pass asked for and served from
CocoIndex's memo.

| Run | What changed | Calls | Reused | Routing | Published | Exclusions | Summary elapsed | Wall |
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
copied corpus with no state database took **148.6 s wall / 148.2 s summary elapsed, 18 calls,
0 reused, 17 published, 313 page records, 2 exclusions** -- the same counters as the CLI cold run
above, because it is the same CocoIndex flow; only the directory and environment handling belong to
the command.

A run on a warm copy (state database restored alongside the corpus, so no Extraction was needed)
measured the command's own overhead: **8.7 s wall / 8.2 s summary elapsed, 0 calls, 18 reused,
17 published, 313 page records, 2 exclusions**. A second unchanged run took **8.5 s wall / 8.2 s
summary elapsed** and rewrote every output byte-for-byte except `out/run-summary.json`, whose
timestamps necessarily change. This is the evidence that the demo command does not wipe the warm
state: the second run did no engine work at all. The same warm root with `--min-characters 5000`
reported **8.7 s wall / 8.3 s summary elapsed, 0 calls, 18 reused, 14 published, 308 page records and
5 exclusions**, so the policy change above also holds through the command.

## The run summary

Each run declares `out/run-summary.json` as target state -- the flow is the only writer. `finepdfs
run` and `finepdfs-demo run` print a one-line view of that same file; `finepdfs-demo status` prints
the last one. Nothing recomputes a counter from the output directory.

Schema version 1:

```json
{
  "schema_version": 1,
  "run": {"started_at": "…Z", "finished_at": "…Z", "elapsed_seconds": 8.863},
  "corpus": {"crawl_id": "CC-MAIN-2026-39", "domain": "…", "crawl_records": 19, "documents": 18},
  "routing": {"documents": 18, "native": 16, "ocr": 2},
  "extraction": {"requested": 18, "calls": 0, "reused": 18},
  "published": {"documents": 14, "page_records": 308},
  "exclusions": {"total": 5, "by_kind": {"dropped": 3, "exact-duplicate": 1, "failed": 0, "near-duplicate": 1}},
  "engine": {"name": "docling", "identity": "docling@cli:docling:v2.140.0"}
}
```

Two things to read carefully:

* `extraction.calls` is counted inside the memoised extract body, so it is one actual engine call and
  never a memo hit. `extraction.reused` is `requested - calls` over memo keys, i.e. observed reuse of
  the run's own work, not a CocoIndex component statistic. The two are deliberately different numbers
  from CocoIndex's "unchanged" line in the progress output.
* `elapsed_seconds` measures the run up to the summary's declaration, before CocoIndex applies target
  state. Wall time around the command is the number that includes the apply. `routing.documents`
  counts routed Documents, so the exact-duplicate record that is skipped before routing is not in it;
  `corpus.documents` is the distinct-digest count.

`exclusions.by_kind` always carries all four kinds (`dropped`, `failed`, `exact-duplicate`,
`near-duplicate`) with zeros, so a reader can depend on the shape. The terminal line prints only the
non-zero kinds.

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
* **No retrieval claim.** 18 Documents, one host, no browser and no search here; the demo
  demonstrates the ingestion architecture, not retrieval quality at corpus scale.
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
