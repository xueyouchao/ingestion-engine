# Glossary

The domain language of the FinePDFs pipeline. Code, tests, specs and tickets use these words and no
synonyms.

**Crawl Record** — one row from the Common Crawl index: a URL, the crawl id, the WARC file name, the
byte offset and length of the record, its MIME type and its HTTP status. A pointer, not a document.

**Corpus** — the set of Crawl Records a run is asked to process, and by extension the directory of PDF
files fetched from them plus its manifest.

**Fetch** — turning a Crawl Record into a PDF file on disk: one HTTP range request, one WARC record
parsed, payload verified. Fetching is separate from processing.

**Document** — one PDF under processing, identified by the digest of its bytes. A Document is not a
Crawl Record: the same Document can be reachable from several Crawl Records, which is exactly what
deduplication is about.

**Page Text** — the text of a single page, with its 1-based page number, as produced by a text layer or
an engine.

**Extraction** — turning Document bytes into an Extracted Document (Page Texts plus engine identity).
Extraction is the expensive step and is memoized.

**Engine** — an adapter that performs Extraction. Four ship: pymupdf (native text layer), docling,
paddleocr-vl, mineru. An Engine is selected per run; its identity is part of the memo key.

**Text Layer Quality** — what the embedded text of a PDF looks like when measured: characters per page,
fraction of pages carrying text, garbled-character ratio, image-dominated pages.

**OCR Routing** — the decision to send a Document to an Engine for real extraction work, or to accept
its text layer. Produced by OcrRoutingPolicy from Text Layer Quality, with a reason.

**OCR Decision** — the routing outcome: needed or not, the features behind it, the thresholds applied,
and a human-readable reason.

**Cleaning** — deterministic text normalisation applied per branch (native text versus OCR text):
boilerplate removal, running titles, hyphenation repair, whitespace. Cleaning is pure domain logic and
is cheap, so it runs on every change.

**Page Language** — the language or script tag identified for one Page Text, with a confidence; the
chosen adapter's vocabulary decides which (`Latn` under `script`, `eng` under `fasttext`).

**Language Mode** — which language adapter a run wires (`script`, the default, or `fasttext`), and so
whether a Page Language names a script or a language. `FINEPDFS_LANGUAGE` selects it.

**Language Policy** — how Page Languages become the Document's language and which pages are dropped
for being below the confidence threshold.

**Duplicate Key** — the content digest that decides exact duplication.

**Similarity Fingerprint** — the MinHash signature that decides near duplication.

**Deduplication** — deciding that a Document is an exact duplicate, a near duplicate (with a
similarity score and the document it matches), or unique. Near-duplicates are grouped transitively:
a chain in which each link is similar to the next is one group, and the smallest URL in a group is the
keeper (ADR-0006).

**Publish Policy** — whether a Document is published, dropped, or excluded, and why.

**Published Document** — a Document that passed routing, cleaning, language and dedup, and is written
out as markdown plus one JSON record.

**Exclusion** — a Document recorded as dropped or failed, with its reason, so nothing disappears
silently.
