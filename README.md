# finepdfs-cocoindex

A PDF curation pipeline shaped like [FinePDFs](https://huggingface.co/datasets/HuggingFaceFW/finepdfs):
take real PDFs from Common Crawl, decide per document whether its text layer can be trusted, read the
ones that cannot with a document vision model, then clean, language-tag, deduplicate and publish them.

It is **incremental**: re-running reads nothing again. Change a cleaning rule and only the cheap half
re-runs; name a different engine and exactly the documents that engine is for are read again.

## Quick start

~~~~bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 1. real PDFs from the newest full Common Crawl, straight from the index into corpus/
.venv/bin/finepdfs fetch-corpus --domain noaa.gov --limit 12

# 2. see what would happen, without writing anything
.venv/bin/finepdfs dry-run

# 3. curate, incrementally
.venv/bin/finepdfs run --corpus corpus --out out
.venv/bin/finepdfs run --corpus corpus --out out    # second time: every extraction is "unchanged"
~~~~

The CocoIndex CLI works too, because the pipeline *is* a CocoIndex app:

~~~~bash
.venv/bin/cocoindex update main.py           # incremental update
.venv/bin/cocoindex update main.py -L        # live: watch the corpus directory
~~~~

## What one document goes through

1. **Fetch** -- the Common Crawl index API gives a WARC file, a byte offset and a length; one HTTP
   range request returns a self-contained gzip member; inside it is the PDF (ADR-0002).
2. **Route** -- measure the embedded text layer (characters per page, page coverage, garbled share,
   image-dominated pages) and decide whether to trust it (ADR-0004).
3. **Extract** -- read it with PyMuPDF's text layer, or with the configured engine: Docling, PaddleOCR-VL
   or MinerU. This is the expensive step, and it is memoised on *document + engine* (ADR-0003, ADR-0005).
4. **Clean** -- different rules for native text and for OCR text: running titles, page numbers, repeated
   table headers, hyphenated line breaks.
5. **Language** -- per-page language, then a document language; pages that disagree are dropped.
6. **Judge** -- minimum pages, minimum characters, allowed languages (ADR-0004).
7. **Publish** -- Markdown plus a JSON record; anything dropped or failed becomes a JSON exclusion.
8. **Decide** -- the second pass sees every cleaned text at once, so it can settle both kinds of
   duplicate in one place: identical bytes from the manifest digests, near-duplicates from text
   fingerprints. What it drops becomes an exclusion; what it keeps is declared (ADR-0006).

## Architecture

Dependencies point inward. The domain knows nothing about HTTP, files or CocoIndex.

| Layer | What lives there | Examples |
|---|---|---|
| `domain/` | value objects and pure policies | `CrawlRecord`, `TextLayerQuality`, `OcrRoutingPolicy`, `CleaningRules`, `LanguagePolicy`, `PublishPolicy`, `plan_deduplication` |
| `application/` | the use cases, expressed against ports | `ExtractionStep` (route + read), `CurationStep` (clean + judge), `CurateDocument` |
| `infrastructure/` | adapters | `commoncrawl/`, `engines/`, `language/`, `publishing/`, `corpus/`, `cocoindex/` |
| `interfaces/` | how it is driven | `interfaces/cli.py`, `main.py` |

Seams you can replace without touching the pipeline:

| Port | Adapter here | Tests use |
|---|---|---|
| `CrawlSource` | `CommonCrawlSource` (index API + ranged WARC reads) | a hand-written fake |
| `TextLayerProbe` | `PymupdfTextLayerProbe` | a fake quality |
| `OcrEngine` | PyMuPDF, Docling, PaddleOCR-VL, MinerU | counting engines |
| `LanguageIdentifier` | `ScriptLanguageIdentifier` | a fixed tag |

## Engines

| Engine | How it is reached | Notes |
|---|---|---|
| `pymupdf` | in process | free, instant, only reads text the PDF already has (AGPL-3.0, so check your use) |
| `docling` | `--engine-command docling` | local CLI, MIT; document-level Markdown, page markers honoured when written |
| `paddleocr-vl` | `--base-url http://host:8000/v1 --model PaddleOCR-VL-1.6` | Apache-2.0; one rendered page per request, prompt `OCR:` |
| `mineru` | `--base-url http://host:8000` (multipart `/file_parse`) or `--engine-command mineru` | MinerU2.5-Pro weights are Apache-2.0; the package carries its own licence |

Serve a model the way its own documentation says, then point the CLI at it:

~~~~bash
vllm serve PaddlePaddle/PaddleOCR-VL-1.6 --trust-remote-code
.venv/bin/finepdfs dry-run --engine paddleocr-vl --base-url http://127.0.0.1:8000/v1 --model PaddleOCR-VL-1.6

vllm serve opendatalab/MinerU2.5-Pro-2605-1.2B --logits-processors mineru_vl_utils:MinerULogitsProcessor
.venv/bin/finepdfs run --engine mineru --base-url http://127.0.0.1:8000
~~~~

## Configuration

Every run is configured by environment variables, because `cocoindex update main.py` takes no
arguments of its own. The CLI flags above simply set them.

| Variable | Default | Meaning |
|---|---|---|
| `FINEPDFS_CORPUS` | `corpus` | corpus directory (manifest plus PDFs) |
| `FINEPDFS_OUT` | `out` | output directory |
| `FINEPDFS_DB` | `cocoindex.db` | CocoIndex state (an LMDB directory) |
| `FINEPDFS_ENGINE` | `pymupdf` | engine for documents whose text layer is unusable |
| `FINEPDFS_ENGINE_BASE_URL` | -- | OpenAI-compatible `/v1` server, or MinerU's base URL |
| `FINEPDFS_ENGINE_MODEL` | -- | model name to ask the server for |
| `FINEPDFS_ENGINE_API_KEY` | -- | bearer token for the server |
| `FINEPDFS_ENGINE_COMMAND` | -- | local command for Docling or MinerU |
| `FINEPDFS_ENGINE_DPI` | `200` | page render resolution for vision models |
| `FINEPDFS_ENGINE_TIMEOUT` | `300` | seconds per model request |
| `FINEPDFS_MIN_CHARACTERS_PER_PAGE` | `200` | routing floor |
| `FINEPDFS_MIN_TEXT_COVERAGE` | `0.5` | routing floor |
| `FINEPDFS_MAX_GARBLED_RATIO` | `0.05` | routing ceiling |
| `FINEPDFS_LANGUAGE_CONFIDENCE` | `0.65` | per-page language floor |
| `FINEPDFS_MIN_CHARACTERS` | `200` | publish minimum |
| `FINEPDFS_LANGUAGES` | all | comma-separated allow-list, for example `Latn,Grek` |
| `FINEPDFS_SIMILARITY_THRESHOLD` | `0.8` | fingerprint similarity above which one document is a near-duplicate of another |

## Output layout

~~~~
out/
  docs/<key>.md          cleaned text of one published document
  records/<key>.json     url, digest, engine, routing decision, language, counts
  excluded/<key>.json    kind (exact-duplicate | near-duplicate | dropped | failed) and the reason
  dedupe/kept.jsonl      the URLs this run decided to publish
  dedupe/duplicates.jsonl  every duplicate, what it duplicates and how alike they are
~~~~

`<key>` is the first 16 hex digits of the SHA-256 of the document URL, so a document never collides with
another and two runs write the same names.

The corpus directory holds `manifest.json` (URL, digest, WARC location) and `pdfs/<sha256>.pdf` -- bytes
named by their own digest, which is also what makes exact duplicates free to spot.

## Two things worth knowing before you run it

*The state database is disposable.* `cocoindex.db` holds the memo table and the target state. Deleting
it costs a re-read of every document and nothing else, and you should delete it after changing the shape
of the flow (as this branch did when the deciding pass was introduced): a state database written by an
older component tree can leave declarations that no current component owns.

*The first pass is concurrent, the second is one component.* Reading runs per document and is memoised;
deciding runs once and holds every text in memory. That is what makes a corpus-wide duplicate decision
possible, and it is the reason this pipeline is aimed at corpora of thousands of documents rather than
the whole crawl.

## Tests

~~~~bash
.venv/bin/python -m pytest              # 94 tests, no network
.venv/bin/python -m pytest -m network   # 4 more against the live Common Crawl (deselected above)
~~~~

## What is verified, and how

* **Real Common Crawl data**: the corpus fetch (12 PDFs, `CC-MAIN-2026-39`), the incremental flow (12
  documents published; the second run reported `12 unchanged`), the dedup pass, and the CLI, all run
  here. The WARC parser is checked against a real 15 KB ranged record whose payload length matches the
  `Content-Length` the origin server sent.
* **Model servers**: the PaddleOCR-VL and MinerU HTTP adapters are exercised against a real stub HTTP
  server on localhost, which proves the wire format, the multipart body and the response parsing.
* **Command-line engines**: Docling, MinerU and PaddleOCR-VL CLI adapters are exercised with a recording
  runner that writes the files those tools write, which proves the command and the parsing around it.
* **Not executed here**: real Docling, PaddleOCR-VL and MinerU weights. This machine has no GPU and none
  of those tools installed, so their adapters are written to the tools' documented interfaces and tested
  against recorded shapes rather than live runs.
* **Language**: the default adapter names the *script* (Latn, Grek, Cyrl), not the language. It cannot
  tell English from German. Swapping in a fastText or GlotLID adapter behind `LanguageIdentifier` is
  the intended upgrade.

## Documents

* `docs/spec/finepdfs-integration.md` -- the specification this was built from
* `docs/GLOSSARY.md` -- the vocabulary, kept deliberately small
* `docs/adr/0001..0006` -- the decisions, with the alternatives that lost
* `.scratch/finepdfs-cocoindex/issues/` -- the tickets the work was sliced into
* `research/` -- the research behind the design, including `paddleocr-vl-mineru-adapters.md`
* `research/diagrams/` -- validated architecture diagrams of the FinePDFs pipeline
