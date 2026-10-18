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

**Source Identity** — where a Document came from, named by its source URL and never by its bytes: the
first 16 hex digits of the SHA-256 of the URL, the `doc_id` of its page records and the key its declared
outputs are named after. Replacing the bytes changes the Document and its digest but not its Source
Identity, so the same exports are rewritten in place; removing the Crawl Record retracts them. A
Document is identified by its digest, a Source Identity by its origin.

**Page Text** — the text of a single page, with its 1-based page number, as produced by a text layer or
an engine.

**Page Record** — one accepted Page Text as a reader cites it: Source Identity, source URL, PDF digest,
original 1-based page number, text and Engine Identity. A Published Document exports its accepted pages
as page records; its Markdown is a projection of the same pages and is not the source of a citation.

**Chunk** — a citable slice of one accepted Page Text, exported by a Published Document as declared
target state (`out/chunks/<key>.jsonl`). A Chunk carries the Source Identity, source URL, PDF digest,
original 1-based page number, Engine Identity and language of the page it came from, plus its own
deterministic Chunk Identity, ordinal, character offsets and chunking version. A Chunk never spans
pages, so its page number is exact.

**Chunk Identity** — the stable `chunk_id` of a Chunk: a hash of the Source Identity, PDF digest,
original page number, ordinal, offsets and chunking version. A replacement, a version bump or a changed
slice moves it, so stale evidence can be retracted instead of duplicated.

**Search Index** — the derived lexical index over currently declared Chunks: SQLite FTS5, the one
shipped backend (ADR-0008). It stores, ranks and cites only what the flow declared; it is not a writer
of accepted evidence and re-decides neither Deduplication nor Publish Policy.

**Search Hit** — one Chunk returned for a query: the Chunk's citation fields, its text, a highlighted
snippet and the BM25 score. The browser and the agent read this same shape, so a backend change cannot
change what a citation means.

**Extraction** — turning Document bytes into an Extracted Document (Page Texts plus Engine Identity).
Extraction is the expensive step and is memoized.

**Engine** — an adapter that performs Extraction. Four ship: pymupdf (native text layer), docling,
paddleocr-vl, mineru. An Engine is selected per run; its Engine Identity is part of the memo key.

**Engine Identity** — an engine's name and its detail, and what the memo key remembers about the Engine:
the name says which Engine ran, the detail names the deployment behind it. A model, page DPI or tool
version change moves the detail and re-reads the corpus; a rented worker's new address does not
(ADR-0005, ADR-0007).

**GpuProvider** — the port that supplies the Endpoint an Engine is reached at for a run and the Lease
that holds it. Three adapters ship: none (the offline default, which supplies nothing and rents
nothing), static (a server that already exists and outlives the run) and command (a start command that
rents a Worker, a stop command that gives it back). "Provider" here always means this port (ADR-0007).

**Vendor** — who rents the GPU, if anything is rented: Modal today, RunPod later. A Vendor lives inside
a GpuProvider's command or adapter, and nothing in the pipeline names one.

**Worker** — the rented process that serves an Engine for one Lease: started by a GpuProvider's start
command, given back by its stop command, and gone when the run ends. A Worker is not an Engine: the
Engine is the adapter, the Worker is where the weights run.

**Sandbox** — Modal's unit of a Worker: one detached process created per Lease, reachable through its
own encrypted tunnel and terminated by id. A Sandbox is not a deployment: nothing serves between runs.
Other Vendors have their own name for the same role.

**Lease Identity** — the id a GpuProvider gives one Lease and passes to both of that Lease's commands,
so a start and its stop find the same Worker and two runs never share one.

**Lease State** — the on-disk record of one Lease: the GpuProvider that owns it and the Worker id to
terminate. Written before the Worker is known to be ready, removed once it is given back, and named
after the Lease Identity so two runs cannot read, clear or stop each other's.

**Lease Reservation** — a start's exclusive hold on one Lease State file, taken before that file is read
and released once the Worker id is recorded. It keeps two starts that would name the same file from both
passing the check and renting a Worker, and it is not durable state: a reservation whose holder is gone is
reclaimed by the next start, so a crash never blocks the Lease.

**Lease** — the hold a run takes on a GpuProvider's Endpoint, released on the way out, including when
the run fails: a rented Worker is given back either way, because a leaked GPU bills by the hour.

**Endpoint** — the bundle a GpuProvider supplies for a run: the base URL, model and API key where the bytes
go right now, and the detail the memo remembers about the deployment behind them. The address moves with
a rented worker; the detail must not move with it. It is the detail half of the Engine Identity, whose
name says which Engine ran (ADR-0007).

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
