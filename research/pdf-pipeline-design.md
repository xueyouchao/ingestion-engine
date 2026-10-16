# Design — a big-data PDF ingestion pipeline on CocoIndex

*Companion to cocoindex-research.md and ideas-plain-words.md (Idea B, made concrete).*

---

## 0. What you're building, in one paragraph

A **continuously-fresh, citable semantic index over millions of PDFs**: a PDF lake in S3 (or a local folder) → parsed **once** to markdown → chunked → embedded → a store that supports hybrid search (vector + full-text), with a graph layer added later. The engine remembers everything it has already done, so you pay per *changed document*, never per *run*.

---

## 1. Architecture: two stages, deliberately

~~~
                    STAGE A (expensive, run once per PDF)          STAGE B (cheap, re-runnable)
+--------------+   +-------------------------------------+   +----------------------------------+
| PDF lake     |   | docling PDF->markdown (memo=True)   |   | RecursiveSplitter -> chunks      |
| S3 / localfs |-->| runner=coco.GPU                     |-->| embed (batching=True)            |--> targets
| walk_dir     |   | artifact = {sha256}.md on disk/S3   |   | declare rows + lineage           |
+--------------+   +-------------------------------------+   +----------------------------------+
                        markdown corpus (durable cache)          Postgres+pgvector / Doris /
                                                                   LanceDB / Neo4j / Kafka
~~~

Why split it:

- **Parse cost is ~100x embedding cost.** Stage A is the expensive thing; memoize it and never pay it twice for the same bytes.
- **The markdown corpus is a durable cache and a debuggable artifact.** When chunking looks wrong, you inspect the markdown, not the PDF.
- **Different invalidation clocks.** Change the chunk size or embedding model → only Stage B re-runs (Stage A's inputs and code are unchanged). A PDF is edited → only that file's chain re-runs. Change docling options → Stage A re-runs (its code changed) and Stage B correctly re-derives.
- This composes the repo's own examples: examples/pdf_to_markdown then examples/pdf_embedding (both: docling → markdown → RecursiveSplitter → embed → pgvector).

---

## 2. Where the PDFs come from

| Source | What you get | How to pull | License / gotcha |
|---|---|---|---|
| **arXiv** | Millions of papers, full PDF set | Requester-pays S3 bucket (arxiv) — info.arxiv.org/help/bulk_data_s3.html | Indexing is fine; you may **not redistribute full text** — link back to arXiv |
| **Hugging Face FinePDFs** (HuggingFaceFW/finepdfs) | Web-scale PDFs from CommonCrawl snapshots, text already extracted | Hub datasets / parquet | ODC-By; pre-extracted text lets you skip Stage A for part of the corpus |
| **Common Crawl** | Raw web-scale; you filter application/pdf out of WARC | WARC files; extract PDF payloads | Free; you do extraction and filtering |
| **PMC Open Access Subset** | Full-text biomedical literature | Bulk from NCBI (pmc.ncbi.nlm.nih.gov/tools/textmining/) | Per-article licenses (mostly CC variants) — check the manifest |
| **SEC EDGAR** | 10-K/10-Q/8-K, exhibits, PDFs | Full-Text Search API + archives | Rate-limited; follow fair-access policy, set a proper User-Agent |
| **govinfo (GPO)** | Federal Register, Congressional Record, bills, CFR — all PDF | Bulk data repo + API (govinfo.gov) | US government works, generally free to use |
| **CourtListener / RECAP** | US court opinions + filings | Bulk downloads | Court opinions are public domain; check terms for filings |
| **EUR-Lex / EU open data** | EU legislation, judgments | Portal bulk downloads | EU open-data license |
| **Security corpus** (your domain) | CISA advisories, NIST SP 800 series, ENISA reports, vendor threat reports | Mostly agency-site PDFs | US gov works public domain; vendor reports copyrighted — index internally, link back |

For API-shaped sources (EDGAR, CourtListener), a plain async function over the HTTP API works as a source with no special connector — the HackerNews example proves the pattern.

---

## 3. Sizing and cost math (parameterized — sanity-check against your own corpus)

Assume **1M PDFs x 10 pages = 10M pages**:

| Step | Estimate | Note |
|---|---|---|
| docling parse | ~0.5-2 s/page CPU → 1,400-5,600 CPU-hours | Dominates everything. Memoize; pay once. Performance is corpus/hardware-dependent — benchmark yours. |
| Chunks | ~30k chars/PDF → ~20 chunks/PDF → ~20M chunks | 2,000-char chunks, 500 overlap |
| Embed (local MiniLM) | order of 1-2k chunks/s on one GPU → a few GPU-hours | Cost: 0 dollars forever after |
| Embed (hosted, e.g. text-embedding-3-small) | ~500 tokens/chunk → ~10B tokens → ~200 USD one-time | Near-zero on later runs — only new docs embed |
| Vector storage | 384-dim float32 ~1.5 KB/vec → ~30 GB for 20M chunks (+ text) | LanceDB/Doris more compact than naive pgvector |
| LMDB state | memo cache ~ the size of your markdown text | The docs warn memoized return values dominate. Set COCOINDEX_LMDB_MAP_SIZE up front |

Headline: **the first run costs real money/time; every subsequent run costs nearly nothing.** That inversion — not raw throughput — is the big-data value of this framework.

---

## 4. Skeleton (real v1 API, adapted from the repo's own pdf_embedding example)

Stage A — PDF → markdown, content-addressed so duplicates collapse:

~~~python
import hashlib, io, pathlib, functools
import cocoindex as coco
from cocoindex.connectors import localfs
from cocoindex.resources.file import PatternFilePathMatcher
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import DocumentStream, InputFormat

MD_DIR = pathlib.Path("./pdf_markdown")

@functools.cache
def converter() -> DocumentConverter:
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption()})

@coco.fn(memo=True)                       # never parse the same bytes twice
def pdf_to_markdown(content: bytes) -> str:
    stream = DocumentStream(name="input.pdf", stream=io.BytesIO(content))
    return converter().convert(stream).document.export_to_markdown()

@coco.fn
async def stage_a(file: localfs.File) -> None:
    data = await file.read()
    sha = hashlib.sha256(data).hexdigest()
    md = await pdf_to_markdown(data)
    localfs.declare_file(MD_DIR / (sha + ".md"), md, create_parent_dirs=True)
    # content-addressed name: the same PDF under any path or S3 key = same artifact

@coco.fn
async def app_a(sourcedir: pathlib.Path) -> None:
    pdfs = localfs.walk_dir(
        sourcedir, recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=["**/*.pdf"]),
        live=True,   # local source supports watch; pass -L to run live
    )
    await coco.mount_each(stage_a, pdfs.items())
~~~

Stage B — markdown → chunks → vectors → Postgres (hybrid: pgvector + full-text):

~~~python
@dataclass
class ChunkRow:
    id: int
    source: str          # pdf sha / original path
    text: str
    chunk_start: int
    chunk_end: int
    embedding: Annotated[NDArray, EMBEDDER]   # dims inferred from the embedder ContextKey

@coco.fn(memo=True)
async def process_markdown(file: FileLike, table: postgres.TableTarget[ChunkRow]) -> None:
    text = await file.read_text()
    id_gen = IdGenerator()
    await coco.map(
        process_chunk,
        _splitter.split(text, chunk_size=2000, chunk_overlap=500, language="markdown"),
        file.file_path.path, id_gen, table,
    )

@coco.fn
async def process_chunk(chunk, source, id_gen, table) -> None:
    table.declare_row(row=ChunkRow(
        id=await id_gen.next_id(chunk.text),
        source=str(source),
        text=chunk.text,
        chunk_start=chunk.start.char_offset, chunk_end=chunk.end.char_offset,
        embedding=await coco.use_context(EMBEDDER).embed(chunk.text),
    ))

@coco.fn
async def app_b() -> None:
    table = await postgres.mount_table_target(
        PG_DB, table_name="pdf_chunks",
        table_schema=await postgres.TableSchema.from_class(ChunkRow, primary_key=["id"]),
    )
    table.declare_vector_index(column="embedding")
    table.declare_sql_command_attachment(
        name="text_fts",
        setup_sql="CREATE INDEX text_fts ON pdf_chunks USING gin (to_tsvector('english', text))",
    )
    mds = localfs.walk_dir(MD_DIR, recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=["**/*.md"]))
    await coco.mount_each(process_markdown, mds.items(), table)

@coco.lifespan
async def lifespan(builder: coco.EnvironmentBuilder) -> AsyncIterator[None]:
    async with asyncpg.create_pool(DATABASE_URL) as pool:
        builder.provide(PG_DB, pool)
        builder.provide(EMBEDDER, SentenceTransformerEmbedder("all-MiniLM-L6-v2"))
        builder.settings.db_path = pathlib.Path("./cocoindex.db")
        yield
~~~

Run: "cocoindex update main.py:app_a" then "cocoindex update main.py:app_b" (add -L for live watching of the local source). Swap localfs.walk_dir for amazon_s3.list_objects(client, bucket, prefix="pdfs/", ...) when the lake lives in S3.

Query side — hybrid, RRF-fused (the same shape as the repo's SEC-EDGAR-on-Doris recipe, done in Postgres):

~~~sql
WITH vec AS (SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1) r FROM pdf_chunks),
     fts AS (SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(to_tsvector('english', text), q) DESC) r
             FROM pdf_chunks, plainto_tsquery('english', $2) q
             WHERE to_tsvector('english', text) @@ q)
SELECT c.* FROM (
    SELECT id, MIN(r) AS rank FROM (SELECT * FROM vec UNION ALL SELECT * FROM fts) t
    GROUP BY id ORDER BY rank LIMIT 20) top
JOIN pdf_chunks c USING (id);
~~~

---

## 5. Big-data mechanics (what actually matters at scale)

**Parallelism** — the engine manages it; don't hand-roll semaphores. Add batching=True to the embedding fn so concurrent misses coalesce into one model call (the engine's own benchmark uses exactly this), and runner=coco.GPU / configure_gpu_pool so embedders and parsers don't fight over one GPU.

**Dedup** — name artifacts by sha256 (above): the same PDF re-crawled from two sources writes the same markdown file and the same rows, and target-state reconciliation cleans up the rest. Cheapest reliable dedup there is.

**Quarantine** — real corpora contain broken scans and encrypted PDFs. Register an exception handler (see advanced_topics/exception_handlers) that routes failures to a quarantine table; don't let 0.1 percent of bad files block a 1M-file run.

**Lineage** — keep source, chunk_start, chunk_end (and page numbers if you extract them) on every row, so every search hit cites its exact origin. That is what makes the index trustworthy, not merely searchable.

**PII** — scrub before chunking if your corpus has it (the SEC-EDGAR example redacts *before* the index so PII never enters it).

**Scale-out reality (important)** — CocoIndex state is a per-process LMDB. Multiple machines do **not** share one store. To go multi-machine: partition the source (one S3 prefix per worker, each with its own LMDB and its own target tables), then merge at query time or with a final fan-in table. This is the single biggest architectural constraint to accept up front.

**S3 freshness** — the S3 connector is poll/list-based. "Fresh" means running "cocoindex update" on a schedule (a cron that only pays the delta), not a push feed. If you need push-freshness, put new-file events on Kafka (kafka.topic_as_map) and mount_each over the feed.

**State sizing** — the memo cache will be roughly the size of your markdown text. Set COCOINDEX_LMDB_MAP_SIZE up front (it auto-grows, but re-climbing from 4 GiB on a 500 GB store on every restart is annoying), and don't memo functions that return giant blobs you don't need cached.

**Failure and resume** — the engine retries transient failures and resumes from its own progress after a crash. Don't wrap the pipeline in your own retry/replay logic; that is the framework's job.

---

## 6. Build plan

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 — weekend | 10k local PDFs → Stage A → Stage B → Postgres+pgvector + FTS | Hybrid query returns hits with exact source citations; re-running is near-instant |
| 2 — ~2 weeks | S3 lake, 100k-1M docs, Doris or LanceDB target, GPUPool + batching, sha-dedup, quarantine, lineage checks with "cocoindex show --tree" | Full backfill within budget; daily catch-up touches only the delta |
| 3 — ongoing | Entity resolution (authors/orgs/products), Neo4j graph layer, Kafka feed for new arrivals, MCP query server for agents | "What does this chunk relate to?" is one query; new PDFs searchable within minutes |

---

## 7. Target-store cheat sheet

| If you need… | Use |
|---|---|
| One box, zero infra, good hybrid search | Postgres + pgvector + to_tsvector |
| 10M+ chunks + analytics + hybrid (vector + inverted, RRF) | Apache Doris (the SEC-EDGAR example is this exact recipe) |
| Pure vector search, cheap, embedded, no server | LanceDB (or in-process zvec) |
| Managed enterprise SQL for analysts | Snowflake or BigQuery (managed DDL, MERGE upserts) |
| "How are these things related?" queries | Neo4j / FalkorDB |
| Fresh alerts derived from the corpus | Kafka target |

---

## 8. Sources

- Repo: examples/pdf_embedding (docling → splitter → pgvector reference), examples/pdf_to_markdown, examples/sec_edgar_analytics (hybrid Doris + RRF + PII scrub), examples/amazon_s3_embedding, examples/text_embedding*.
- Docs: connectors (S3, Postgres, Doris, LanceDB, Kafka), advanced_topics (internal_storage, exception_handlers, timeouts, progress_monitoring, concurrency_control), ops/text.
- External: arXiv bulk S3 (info.arxiv.org/help/bulk_data_s3.html), Hugging Face HuggingFaceFW/finepdfs (ODC-By, from CommonCrawl snapshots), PMC OA (pmc.ncbi.nlm.nih.gov/tools/textmining/), govinfo bulk data, CourtListener bulk, SEC EDGAR fair-access policy.

---

## Appendix: why the official pdf-embedding example is only ONE domain step

*Verified by fetching cocoindex.io/docs/examples/pdf-embedding/ (page metadata: Last reviewed
Jul 4, 2026; badges Starter / Production ready). The observation is correct — and the page itself
is explicit about it.*

### What that example actually contains

One domain step; everything else is machinery:

- docling PDF to Markdown, built once via functools.cache, wrapped as
  coco.fn.as_async(runner=coco.GPU) so the expensive parse does not block the event loop.
- RecursiveSplitter chunking (2000 / 500, language markdown).
- SentenceTransformerEmbedder(all-MiniLM-L6-v2) provided through a ContextKey with detect_change=True.
- Postgres + pgvector target via mount_table_target; rows declared with
  Annotated[NDArray, EMBEDDER] so vector dimensions are inferred from the embedder.
- live=True on walk_dir, memo=True on the per-file component, mount_each for one component per PDF.
- Chunk row id derived from the chunk text, so a chunk that survives a re-parse keeps its row.
- No vector index. The page says so directly: to keep the example minimal it does not declare a
  vector index, so queries do a sequential scan — fine for a few PDFs; for a larger corpus, add one
  line, declare_vector_index(column='embedding').

So the domain surface is deliberately minimal — a teaching example, not a corpus builder. Compare
with FinePDFs, which routes between docling and rolmOCR depending on OCR need, handles truncated
and corrupted PDFs, cleans pages, runs language ID, and does two kinds of dedup.

### But the same page documents the machinery a batch recipe does not provide

Its section 10, Incremental updates, is the actual payload — and it is exactly the benefit list for
porting FinePDFs-style steps into CocoIndex:

- add a PDF -> only that file is parsed, chunked, embedded; the rest untouched;
- replace a PDF -> re-parsed and re-chunked; chunks whose text is unchanged keep their id and
  embedding; genuinely new chunks are embedded; vanished chunks are deleted;
- delete a PDF -> its rows are removed automatically;
- logic changes (chunk size, embedding model) -> the engine compares new output against what is
  already in Postgres and applies only the difference.

memo=True decides what to recompute (content hash + code hash); the managed target decides what to
write (idempotent upserts, orphan cleanup). A batch pipeline has to hand-write both.

### Porting FinePDFs boxes into that structure

| FinePDFs box | Where it goes in the example's shape |
|---|---|
| Truncation identification | per-document check inside the per-file function |
| Corrupted-PDF removal | try/except plus a quarantine table (exception handlers) |
| Needs-OCR branch | a plain if in the per-file function; both branches memoized |
| rolmOCR / docling extraction | same shape as pdf_to_markdown — a sync fn under coco.fn.as_async(runner=coco.GPU), or a hosted OCR API call |
| Boilerplate / page-number / repetition cleanup | ordinary text functions between parse and chunk |
| GlotLID language ID | a memoized function wrapping the LID library (no built-in natural-language LID) |
| Model-based filtering | a memoized, batched classifier or LLM call |
| Exact + MinHash dedup | outside — corpus-global; no shuffle/join in the framework |
| Refetch truncated PDFs | outside — a side effect producing new inputs; materialize into the source bucket |

### The takeaway

The docs optimize for teaching the model — source to transform to target, memoization, lineage —
with the smallest possible domain surface. Production depth is your code either way. What you buy by
writing it inside CocoIndex rather than a batch script is that re-running costs the delta, not the
corpus: the OCR/extraction box (the expensive one) is fingerprinted by content, so tuning a
downstream filter does not re-OCR the corpus.
