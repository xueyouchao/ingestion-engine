# CocoIndex — Research Brief & Application Ideas

*Primary-source research. All claims below are traced to the upstream repository or the published package, not to secondary write-ups.*

| Fact | Value | Source |
|---|---|---|
| Upstream repo | `github.com/cocoindex-io/cocoindex` | repo root |
| Commit studied | `4fc75a418f915f6962028eea060e439728426f21` (2026-10-15) | `git rev-parse HEAD` in clone |
| Core package | `cocoindex` **1.0.25** (PyPI, wheel `cp311-abi3-manylinux_2_28_x86_64`) | `pip download cocoindex` |
| Flagship product | `cocoindex-code` **0.2.42** (`ccc` CLI / MCP server) | `pip download cocoindex-code` |
| License | Apache-2.0 | `pyproject.toml`, dist METADATA |
| Python | `>=3.11` (pyproject / METADATA). README badge says 3.10–3.13 — **discrepancy**; trust `>=3.11` | `pyproject.toml:11`, wheel METADATA |
| Stack | Python SDK over a Rust engine | `rust/{core,ops_text,code_ast,code_match,sdk,py}` |
| Bundled examples | ~40 directories under `examples/` | `ls examples` |

**Note on placement:** this repository/workspace had no existing notes convention (it was empty), so this file lives at `research/cocoindex-research.md`. The upstream repo was cloned for reading only; all `path` citations are relative to that clone, not to this workspace.

---

## 1. TL;DR

CocoIndex is **"React for data engineering"**: you declare *what the target should look like as a function of the source*, and a Rust engine figures out the delta and keeps external systems in sync — forever, with only changed work re-run.

Its differentiator is not vector search (everyone has that). It is **fine-grained incremental recomputation with end-to-end lineage**:
- component level — only changed source items are reprocessed,
- function level — expensive calls (embeddings, LLM extraction) are memoized on `hash(input) + hash(code)`,
- target level — the minimum insert/update/delete set is applied to the destination.

This makes it a strong fit whenever you have a **large, slow-changing corpus** that must stay **fresh and traceable**: security research corpora, lakehouse enrichment, live game content, domain knowledge bases. It is *not* a replacement for Spark/Flink-style shuffle analytics; it is the ingestion/enrichment/materialization layer around them.

The single best proof that the model works in production is the vendor's own flagship: **`cocoindex-code`**, an AST-aware semantic code index shipped as an MCP server for coding agents (`cocoindex_code/`, PyPI `cocoindex-code` 0.2.42).

---

## 2. What it is (and what it is not)

> "CocoIndex is an ultra-performant framework for building data processing pipelines for AI workloads, with built-in incremental processing."
> — `docs/src/content/docs/getting_started/overview.mdx`

Core properties (same doc):
- **High-performance Rust engine** — pipelines execute on a Rust core, parallel by default.
- **Easy to code** — plain Python, no DSL; write batch-style code, the engine runs it incrementally.
- **Incremental & low-latency** — avoids recomputing based on multi-level change detection; updates drop from hours/days to seconds.
- **Full lineage & explainability** — every step, intermediate result, and execution path is inspectable; the docs explicitly tie this to EU AI Act transparency and enterprise auditability.
- **Open integration model** — sources/targets plug in via a standard interface; you use the Python ecosystem for models.
- **High throughput + controlled concurrency** — managed concurrency and request batching (e.g. `batching=True`) to cut GPU/RPC fanout.
- **Fault-tolerant runtime** — retries transient failures and resumes from progress after interruption, so no manual backfills.
- **Low operational overhead** — refresh, state, backfill, correctness, GPU coordination, scaling are engine concerns.

What it is **not**:
- Not a distributed SQL / shuffle engine. Heavy joins and aggregations belong in Doris / BigQuery / Snowflake / Postgres; CocoIndex drives them as targets.
- Not a scheduler/orchestrator (no Airflow replacement). It runs as a process/library; live mode keeps one process long-running.
- Not tied to one vector DB — targets are pluggable (see §4).

---

## 3. Mental model & the API in 60 seconds

**`TargetState = Transform(SourceState)`.** You declare desired output; the engine reconciles. Analogies the docs use: React (declare UI as a function of state), spreadsheets (formulas recompute when cells change), materialized views (`overview.mdx`).

Canonical minimal pipeline (README + `skills/cocoindex/SKILL.md`):

```python
import cocoindex as coco
from cocoindex.connectors import localfs, postgres

@coco.fn(memo=True)                 # cached by hash(input) + hash(code)
async def index_file(file, table):
    for chunk in splitter.split(await file.read_text()):
        table.declare_row(text=chunk.text, embedding=embed(chunk.text))

@coco.fn
async def main(src):
    table = await postgres.mount_table_target(PG, table_name="docs")
    table.declare_vector_index(column="embedding")
    await coco.mount_each(index_file, localfs.walk_dir(src).items(), table)

coco.App(coco.AppConfig(name="docs"), main, src="./docs").update_blocking()
```

The concepts actually exported (`python/cocoindex/_internal/api.py`, `__all__`), which matter when reading real code:

| Concept | API | Purpose |
|---|---|---|
| App | `App`, `AppConfig`, `update_blocking`, `update`, `drop` | Top-level bound to a `main` fn + params |
| Processing fn | `@coco.fn(memo=, version=, batching=, runner=coco.GPU)` | Participates in the pipeline; `memo=True` caches |
| Components | `mount`, `mount_each`, `use_mount`, `map`, `component_subpath` | Group an item's processing; stable path = correct memo + cleanup |
| Target state | `declare_row`, `declare_file`, `declare_target_state`, `declare_vector_index`, `declare_relation` | Declares desired output; engine diffs it |
| Shared resources | `ContextKey`, `EnvironmentBuilder`, `@coco.lifespan`, `use_context` | Provide DB pools, models once; `detect_change=True` invalidates memos when the value changes |
| Live streaming | `LiveComponent`, `LiveMapView`, `LiveMapFeed`, `auto_refresh` | Continuous updates after catch-up (`update_blocking(live=True)`, CLI `-L`) |
| Runners | `Runner`, `GPU`, `GPUPool`, `GPURunner`, `current_gpu` | GPU-bound work serialized/pooled |
| Lineage & state | `StablePath`, `StableKey`, `use_state`, LDMB via `Settings`/`LmdbSettings` | Inspectable state and stable identity across runs |

**Change detection** (`advanced_topics/memoization_keys.mdx`): function args, `deps` values, and `detect_change=True` context values are fingerprinted through one pipeline. Precedence: `__coco_memo_key__()` → registered memo-key function for the type → structural canonicalization of primitives/containers/dataclasses/Pydantic v2 → pickle fallback. That is *why* code edits invalidate only dependent memos.

**Internal state** (`advanced_topics/internal_storage.mdx`): target states and memo results persist in **LMDB**. `map_size` (default 4 GiB) auto-grows on overflow; `max_dbs` is a hard cap. The docs warn that memoized return values — e.g. embedding vectors — usually dominate store growth.

**Live vs catch-up** (`skills/cocoindex/SKILL.md`): `update()` is catch-up — scan all sources, process the delta, sync, return. `live=True` keeps running and lets live-capable sources stream changes. Two live source shapes: `LiveMapView` (scan snapshot first, then watch — e.g. `localfs.walk_dir(..., live=True)`) and `LiveMapFeed` (stream only, no snapshot — e.g. `kafka.topic_as_map()`); `mount_each` auto-detects feeds.

---

## 4. Capability inventory (use this to design solutions)

### 4.1 Sources
From `docs/src/content/docs/connectors/index.mdx` and `docs/src/content/docs/connectors/*`:

| Source | Notes |
|---|---|
| Local filesystem (`localfs.walk_dir`) | glob matching, recursive, **live watch mode**, stable `FilePath` |
| Amazon S3 | prefix listing, path matcher, max file size; also S3-compatible (MinIO, Tigris) |
| Azure Blob Storage | containers |
| Google Drive | shared folders via service account |
| OCI Object Storage | event-driven |
| PostgreSQL | `PgTableSource` with row typing |
| Kafka | topics as live keyed maps or raw streams |
| Iggy | live streams/maps |
| Custom | plain `async` functions returning `.items(key=...)` — the HN example uses a public HTTP API as a source with no special decorator |

### 4.2 Targets
| Category | Targets | Vector support |
|---|---|---|
| Relational / analytical | Postgres (pgvector), SQLite (sqlite-vec), Snowflake, BigQuery, Apache Doris | pgvector / sqlite-vec / Doris HNSW+IVF |
| Vector DBs | LanceDB, Qdrant, Turbopuffer, Valkey, zvec | native; single / named / multi-vector |
| Property graph | Neo4j, FalkorDB, SurrealDB | native vector indexes |
| Message streams | Kafka, Iggy | — |
| Files | LocalFS (`declare_file`, `mount_dir_target`) | — |
| Custom | `advanced_topics/custom_target_connector.mdx` | — |

Targets are mounted via `mount_*_target(ContextKey, ...)` (a `ContextKey`, not a raw connection — so identity survives across runs), then rows/points/edges are declared. Warehouses get **managed DDL + MERGE upserts + incremental deletes** (Snowflake/BigQuery pages).

### 4.3 Built-in operations (`python/cocoindex/ops/`, `docs/src/content/docs/ops/index.mdx`)
- **`ops.text`** — `RecursiveSplitter`: **syntax-aware (tree-sitter)** splitting that respects functions/classes/blocks, falls back to separators; returns position-tracked `Chunk`s; `detect_code_language`.
- **`ops.code`** — structural pattern matching over a shared parsed AST: `CodeSource`, `CodePattern`, `match_code`, `index_terms`, `render_match`; metavariables use the `\` sigil. This is the AST-aware code intelligence layer.
- **`ops.sentence_transformers`** — local embeddings, model caching, thread-safe GPU access.
- **`ops.litellm`** — embeddings + audio transcription across 100+ providers via one API.
- **`ops.entity_resolution`** — FAISS embedding similarity + pluggable (built-in LLM) pair resolver, `PINNED`/`PREFERRED` canonical policies, returns `ResolvedEntities` with `.canonical_of()` / `.groups()`. This is *dedup as a first-class op* — directly useful for CVE/vendor/character/company name normalization.

### 4.4 CLI (`docs/src/content/docs/cli_commands.mdx`)
`cocoindex init`, `update [app] [-L live] [--full-reprocess]`, `drop`, `ls`, `show [--tree]`. State lives in `COCOINDEX_DB` / lifespan `builder.settings.db_path`.

---

## 5. Evidence on scale & incremental correctness

`benchmarks/file_summarization/BENCHMARK_REPORT.md` runs the same pipeline in the Rust SDK and Python SDK over generated corpora up to **21,504 files**, in four phases (cold, warm, edit, shape), across io/cpu/mixed profiles. Headline results as reported:

- Rust SDK cold run **30–80× faster** than Python at 10k+ files; peak 81.6× (codebase/cpu, xlarge).
- Warm run **8–10× faster**.
- **Incremental contract held at every scale:** warm phases had `cache_misses = 0`; "shape" phases (add/rename one file) invalidated only **9–15 sections** and rewrote **4–11 output files** — no cascading rebuild.

Caveat: these are the project's own benchmarks, not independently reproduced here. Treat the *shape* (flat incremental cost) as the durable claim, not the exact multipliers.

Real-world proof of the model at product scale: `cocoindex-code` maintains "live call graphs, symbols, vectors, and chunks fresh on every commit," supports Python/TypeScript/Rust/Go, and claims "70% fewer tokens per turn" and "80–90% cache hits on re-index" (vendor README/metadata claims — unverified).

---

## 6. Where the fit is strong vs weak

**Strong fit**
- Corpus of many files/records where a small delta changes often (codebases, docs sites, security reports, design docs, logs).
- Expensive per-item work you don't want to repeat (embeddings, LLM extraction, OCR, transcription).
- Outputs that are stores with upsert/delete semantics (vector DB, graph DB, warehouse, search index).
- Anywhere freshness + provenance + audit trail are requirements, not nice-to-haves.

**Weak fit / avoid**
- Shuffle-heavy analytical compute (joins, window functions, large aggregations) — do it in the warehouse.
- Sub-millisecond request-path serving — CocoIndex maintains the index; your app queries the target store.
- One-shot, throwaway transformations where setup cost dominates.
- Anything needing cross-item global consistency in a single transaction as an invariant — the model is per-component reconciliation.

---

## 7. Application ideas

Each idea is scored on the four things CocoIndex cares about: **incremental win**, **lineage/audit**, **connector fit**, and **time-to-first-demo**.

### Idea A — "Security Research Brain": an always-fresh corpus over code + findings + threat intel

*This is tailored to the local `~/security` workspace, which already contains `repos/` (cloned audited libraries: doppler-cli, matomo, liberapay, paragonie/…), `findings/`, `reports/`, `recon/`, `verify/`, `demo/`.*

**Problem.** Security review generates a large, constantly-mutating corpus: vendored source trees, recon notes, draft findings, PoCs, reports, and external intel (CVE/NVD, CWE, MITRE ATT&CK, advisories, SBOMs). Today an agent re-reads or greps it from scratch each session; past findings aren't linked to the code patterns that caused them, and "which of my audited packages are affected by this new CVE?" is manual.

**Why CocoIndex.** The corpus is huge and 99% static, but you edit findings and pull new commits constantly. That is the exact delta shape CocoIndex is built for. `ops.code` (AST matching) + `RecursiveSplitter(language=…)` gives structure-aware code chunks; `ops.entity_resolution` normalizes CVE/CWE/vendor/package/finding names; the graph targets hold relationships; lineage gives an audit trail for every claim.

**Pipeline sketch**

```
sources                                   transforms                                  targets
──────────────────────────────────────    ────────────────────────────────────────    ────────────────────────────
localfs.walk_dir(~/security/repos,        detect language → RecursiveSplitter         Postgres+pgvector  (hybrid search:
  matcher=**/*.{php,go,py,js,ts})           → embed (sentence-transformers)             chunks + metadata + full-text tsvector/GIN)
localfs.walk_dir(~/security/findings,     markdown chunk → embed                      Neo4j / FalkorDB  (graph:
  matcher=**/*.{md,json})                 LLM extract (instructor + litellm):           CVE–Package–Repo–Finding–CWE–ATT&CK)
localfs.walk_dir(~/security/recon,        {severity, cwe, component, poc, status}     Doris / BigQuery  (analytics over findings)
  matcher=**/*)                           entity_resolution: vendors, packages,       S3/local          (rendered report artifacts)
Postgres (findings table)   [PgTableSource]   malware families, duplicate findings
Kafka (scanner/CI events)   [LiveMapFeed]
external: CVE/CWE/ATT&CK feeds (custom async source)
```

**What it unlocks**
1. *Cross-repo semantic search*: "find anything like this `s ≥ L` scalar-arithmetic bug in our other PHP crypto targets" — same idea as `cocoindex-code`, but over an audit corpus.
2. *Finding-to-code lineage*: every finding points back to the exact source bytes and chunk, satisfying the "drafts for human verification" workflow in `~/security/STATUS.md`.
3. *Dedup*: entity resolution collapses "Doppler"/"doppler-cli"/"doppler cli" and duplicate findings across reports.
4. *Fresh CVE impact*: a new CVE row triggers re-embedding only that record and re-running only the graph edges that touch it — "blast radius" query stays current.
5. *Live mode*: `walk_dir(..., live=True)` on `repos/` + `findings/` means as you edit a draft finding or `git pull`, the index updates in seconds; a Kafka scanner feed gives real-time triage.
6. *MCP surface*: expose it as an MCP server exactly like `ccc mcp`, so a coding agent gets `search` + `refresh_index` over the security brain.

**Incremental win:** add one repo file → one chunk set re-embeds; edit one finding → one doc re-extracts; bump the extraction prompt → only LLM memos whose code hash changed re-run. **Time-to-first-demo:** localfs → Postgres+pgvector → `RecursiveSplitter`, a weekend.

**Risks:** don't index secrets/credentials in `recon/`; scrub PII before chunking (the SEC-EDGAR example shows the pattern — redact *before* the index). Vet that any external intel feed's license permits re-indexing.

**Variant A2 — Threat-intel knowledge graph for a SOC.** Same skeleton, different source mix: NVD/CVE JSON, MITRE ATT&CK STIX, Sigma/YARA rules, advisories, CMDB/asset inventory, SBOMs. Targets: Neo4j (ATT&CK technique → detection rule → asset), Doris (hybrid retrieval + analytics), Kafka (alert stream in, enriched alert stream out). The incremental win is largest on the *asset* side: one new host re-runs only the rules/edges touching it.

---

### Idea B — Big-data processing: incremental lakehouse enrichment & hybrid retrieval

**Problem.** Raw data lands continuously (logs, events, documents, audio) in object storage / Kafka / a warehouse. You need to parse, normalize, scrub PII, dedup entities, embed, LLM-extract, and publish to analytical + vector + graph stores — without reprocessing the world on every batch.

**Why CocoIndex.** It is the *enrichment and materialization* half of a modern lakehouse: Kafka/S3/DB in, Doris/Snowflake/BigQuery/LanceDB/graphs out, with managed DDL, MERGE upserts, and incremental deletes. `batching=True` coalesces per-record calls; `GPUPool` controls GPU cost; LMDB memoization makes warm runs nearly free.

**Pipeline sketch**

```
Kafka topic (LiveMapFeed) ─┐
S3 / Azure Blob raw files ─┼─► parse & normalize ─► PII scrub ─► chunk ─┐
Snowflake/BigQuery tables ─┘                                          ├─► embed ─┐
                                                                      └─► LLM   ├─► Doris      (vector + inverted → hybrid RRF)
                                                                          extract┘─► Snowflake/BigQuery (MERGE upsert)
                                                                                  ─► LanceDB/Turbopuffer (vectors)
                                                                                  ─► Kafka (enriched stream out)
```

Grounding: the repo already ships exactly these building blocks — `examples/sec_edgar_analytics` (multi-format 10-K text + XBRL JSON → one Doris table with **vector + full-text indexes fused by RRF**, PII scrubbed before chunking), `examples/csv_to_kafka`, `examples/kafka_to_lancedb`, `examples/snowflake_target`, `examples/bigquery_target`, `examples/amazon_s3_embedding`.

**Incremental win:** the benchmark's "warm cache_misses = 0" means re-running after a code or config change costs ~nothing for unchanged records; new Kafka messages cost one record each. **Time-to-first-demo:** copy `kafka_to_lancedb`, swap in your topic and schema.

**Risks:** CocoIndex will not replace your Spark/dbt jobs for heavy joins; treat it as the per-record enrichment + index-sync layer. Watch LMDB growth from large memoized embedding vectors — the docs flag this explicitly.

---

### Idea C — Games: a living knowledge base + live-ops pipeline

**Problem.** A studio's truth is scattered across design docs, lore/dialogue tables, tuning CSVs, engine code, localization sheets, and art/audio assets. Designers and writers ask "where is the stamina system defined / what does this NPC know in this patch?" Live-ops needs per-match telemetry streaming into balance analytics. And shipping a *client-side* retrieval index is hard.

**Why CocoIndex.**
- *Editor-time / service-time freshness:* `walk_dir(live=True)` on the design repo keeps an always-current index while designers edit — no nightly build.
- *Structure-aware code search:* `ops.code` + `RecursiveSplitter(language=…)` index the gameplay code and call graph like `cocoindex-code`.
- *Multimodal:* the repo ships `examples/image_search`, `examples/image_search_colpali`, `examples/audio_to_text`, `examples/face_recognition`, `examples/slides_to_speech` — art/voice can enter the same corpus.
- *Ship local:* SQLite / LanceDB / zvec are **embedded** targets, so a build step can bake a retrieval index into the game data — no server dependency.

**Pipeline sketch**

```
design docs (.md/.docx)         ─► chunk ─► embed ─┐
lore/dialogue tables (.csv/.json)─► normalize ──────┼─► Postgres/LanceDB (NPC & designer search)
gameplay code (C++/C#/py)        ─► AST chunk+embed─┤   SQLite/zvec (baked, ships with client)
tuning/balance CSVs              ─► normalize ──────┤   Neo4j (quest/dialogue/lore graph)
art/audio assets (S3/localfs)    ─► CLIP / ASR ─────┘   Kafka → Doris (live-ops balance analytics)
player telemetry (Kafka)         ─► aggregate
```

**Use cases**
1. *Designer copilot*: "show every place the stamina system is defined, tuned, and documented" across docs + code + CSV — grounded, with provenance.
2. *Grounded NPC dialogue*: retrieve lore + current-patch tuning + relevant design intent as RAG context; entity resolution keeps character/item names canonical across teams.
3. *Asset hygiene*: image/audio similarity search finds duplicates, near-duplicates, and unused assets.
4. *Live-ops*: Kafka `LiveMapFeed` → Doris/Snowflake with vector + inverted indexes for hybrid analysis of balance questions.
5. *Localization coverage*: one component per string-table row; changing one locale re-runs one row.
6. *Mod SDK search*: ship a `ccc`-style AST semantic search over the modding API.

**Incremental win:** rewrite one dialogue line → one chunk re-embeds and only the NPC docs that reference it re-sync; retune one CSV column → only affected rows. A full rebuild per design edit becomes a delta. **Risks:** keep the *client-shipped* index small (quantize/trim), and treat any LLM extraction as build-time, not runtime.

---

### Idea D — Specialized knowledge base (the general recipe)

For any vertical — legal, medical, finance, regulatory, academic, internal handbook — the winning shape is the same, and CocoIndex's contribution is *freshness + lineage + cost control*, not just retrieval.

```
multi-format sources            scrub / normalize       enrich                      materialize
PDF, HTML, MD, audio, video  ─► PII scrub ─► chunk ─►  embed ─► LLM extract ─►     Postgres/pgvector  (hybrid search)
Google Drive / S3 / Notion    ─► dedupe entities     ─►  entity_resolution ─►      Neo4j/FalkorDB     (concept graph)
Postgres / APIs               ─►                        (triples)                   Doris/BigQuery     (analytics)
```

Grounded examples already in the repo: `docs_to_knowledge_graph` (Markdown → LLM triples → Neo4j `Document`/`Entity`/`RELATIONSHIP`/`MENTION`), `meeting_notes_graph_{neo4j,falkordb,surrealdb}` (adds entity resolution via `resolve_entities` + `LlmPairResolver`), `conversation_to_knowledge`, `paper_metadata`, `patient_intake_extraction_{baml,dspy}`, `manuals_llm_extraction`, `entire_session_search`.

**Differentiators vs. a one-shot RAG ingester**
1. Add/update a document → only its chunks re-embed.
2. Change the extraction prompt/model → only memos depending on that *code* re-run (pinned with `ContextKey(..., detect_change=True)`).
3. Every answer can be traced to a source byte range (`StablePath` lineage).
4. Multiple materializations (vector + graph + warehouse) stay consistent from one declaration.

**Time-to-first-demo:** `cocoindex init`, point `localfs.walk_dir` at a folder, mount one Postgres table, add `memo=True` to the expensive functions, then extend to a graph.

---

### Idea E (bonus) — Code intelligence & agent memory

The vendor's own product, `cocoindex-code`, is the reference implementation of this: AST chunking → embeddings → SQLite+sqlite-vec, with a CLI (`ccc index`, `ccc search`), an MCP server (`ccc mcp`), and agent hooks that re-index after edits. Building a domain-specific analog is one of the highest-ROI uses of CocoIndex: take the `examples/code_embedding` + `examples/code_embedding_lancedb` pattern, swap Postgres for SQLite/LanceDB, add `ops.code` structural matching for call-graph/blast-radius queries, and wrap it as an MCP tool. The same pattern applied to *conversation history* gives long-horizon agent memory (`examples/entire_session_search`, `examples/conversation_to_knowledge`).

---

## 8. Recommended starter path

1. **Day 1** — `pip install -U cocoindex`; `cocoindex init mybrain`; one source (`localfs.walk_dir`) → one target (`postgres.mount_table_target` + `declare_vector_index`); `RecursiveSplitter` + `sentence-transformers`; `memo=True` on the per-file and per-chunk functions.
2. **Day 2** — add metadata columns (path, mtime, line range, doc type) and a hybrid query (pgvector + `tsvector`); add a second source (S3, Google Drive, or Postgres `PgTableSource`).
3. **Day 3** — add the graph target (Neo4j/FalkorDB) with LLM-extracted triples; add `ops.entity_resolution` to dedupe names.
4. **Day 4** — turn on live mode on a live-capable source (`walk_dir(live=True)` or Kafka); optionally wrap in an MCP server (`mcp` package) modeled on `ccc mcp`.
5. **Ongoing** — pin model/prompt versions with `ContextKey(..., detect_change=True)`, tune `COCOINDEX_LMDB_MAP_SIZE` if the state store grows, and use `cocoindex show --tree` to inspect lineage.

---

## 9. Gotchas & risks (from primary sources)

- **Python ≥ 3.11** per `pyproject.toml`/METADATA; README badge claims 3.10–3.13. Verify before pinning a 3.10 environment.
- **Live mode needs a live-capable source** — `update_blocking(live=True)` alone is not enough. Use `LiveMapView` (`walk_dir(..., live=True)`) or a `LiveMapFeed` (Kafka/Iggy).
- **LMDB state growth**: memoized return values, especially embeddings, dominate. Default `map_size` 4 GiB auto-grows but re-climbs from the configured value on restart; tune it for large deployments (`advanced_topics/internal_storage.mdx`).
- **Memoization depends on stable component paths.** Use `component_subpath("file", str(path))` / `files.items(key=...)`, never object references or loop indices (`skills/cocoindex/references/patterns.md` anti-patterns).
- **No side effects in `@coco.fn`.** Only declare target state; arbitrary `requests.post(...)` is invisible to change tracking.
- **Code/model changes intentionally invalidate memos.** Changing a prompt or embedder re-runs dependents — pin versions and use `version=` when you want to force it deliberately.
- **GPU coordination**: use `runner=coco.GPU` / `configure_gpu_pool` and `batching=True` rather than ad-hoc concurrency.
- **Maturity**: core `cocoindex` is classified Production/Stable at 1.0.25, but `cocoindex-code` is 0.2.x Alpha. Connector maturity varies per store (e.g. some are target-only).
- **Not a distributed compute engine** — keep shuffles/aggregations in the warehouse, use CocoIndex for per-record enrichment and target sync.
- **Compliance**: lineage supports auditability, but you own data-governance (PII scrubbing before indexing; the SEC-EDGAR example demonstrates it).

---

## 10. Sources (primary)

All paths relative to `cocoindex-io/cocoindex@4fc75a418f915f6962028eea060e439728426f21`.

- `README.md` — positioning ("React for data engineering"), quickstart, flagship.
- `docs/src/content/docs/getting_started/overview.mdx` — features, incremental processing levels.
- `docs/src/content/docs/programming_guide/{core_concepts,target_state,function,app,context,live_mode}.mdx` — model & APIs.
- `docs/src/content/docs/connectors/index.mdx` + `connectors/*.mdx` — full source/target matrix, vector support, type mappings.
- `docs/src/content/docs/ops/index.mdx`, `ops/*.mdx` — `text`, `code`, `litellm`, `sentence_transformers`, `entity_resolution`.
- `docs/src/content/docs/advanced_topics/{internal_storage,memoization_keys,multiple_environments,concurrency_control,custom_target_connector}.mdx` — LMDB state, fingerprinting, concurrency, custom targets.
- `docs/src/content/docs/cli_commands.mdx` — CLI surface.
- `skills/cocoindex/SKILL.md` + `references/{patterns,connectors,api_reference,setup_project,setup_database}.md` — condensed API/pattern reference.
- `python/cocoindex/_internal/api.py` (`__all__`) — authoritative export list.
- `python/cocoindex/ops/{code,text,entity_resolution,sentence_transformers,litellm}.py` — op implementations.
- `examples/` — 40 runnable pipelines, notably `sec_edgar_analytics`, `docs_to_knowledge_graph`, `meeting_notes_graph_*`, `kafka_to_lancedb`, `code_embedding*`, `entire_session_search`, `patient_intake_extraction_*`, `snowflake_target`, `bigquery_target`, `image_search_colpali`, `audio_to_text`.
- `benchmarks/file_summarization/BENCHMARK_REPORT.md` — scale & incremental numbers (vendor-run).
- PyPI `cocoindex` 1.0.25 wheel METADATA; PyPI `cocoindex-code` 0.2.42 METADATA/README — published package facts.
- `rust/{core,ops_text,code_ast,code_match,sdk,py}` — Rust engine layout.

### How to reproduce this research
```bash
git clone --depth 1 https://github.com/cocoindex-io/cocoindex.git
git -C cocoindex rev-parse HEAD          # 4fc75a4…
pip download cocoindex --no-deps          # 1.0.25
pip download cocoindex-code --no-deps     # 0.2.42
```
