# Brute force vs. an index: Cheng Lou's 100k-JSON WebGPU search

*Verified against github.com/chenglou/handwired, src/json-search/README.md (38 KB writeup) and the
live demo at chenglou.me/handwired/json-search/. Answers: does this beat Elasticsearch?*

## What it actually is

Part of **Handwired** — a project that compiles ordinary JavaScript (if/else, loops, carried state)
into matmuls plus ReLUs over floats, with hand-written weights, no training, exact, and runnable on
either plain JS or WebGPU. The json-search demo applies that to search:

- **100,000 generated JSON documents**, searched again at every keystroke, on the GPU, with two
  fixed numeric programs: a JSON highlighter and a searcher.
- Regex matching, JSON parsing, and highlighting are all written in ordinary JS and turned into
  **WGSL compute kernels**. The highlighter is itself such a program.

## The numbers (their measurements, Apple M5 Max, Chrome 155)

- Documents: synthetic, deterministic (every choice is a hash of seed + index + counter, no
  Math.random), **up to 512 code points, about 300 characters each**, 7 shapes, 4 writing styles.
- Memory: 513 float32 per document = 2,052 bytes, so 100,000 documents are **205 MB of cells plus
  205 MB of display kinds = 410 MB on the GPU** (448 MB in the CPU worker). The actual text is
  roughly **30 MB**.
- Startup: documents ready 0.54 s after navigation (0.32 s of that writing them); first answer
  painted at 0.56 s.
- A search: **11-15 ms per keystroke** as the page reports it (compile pattern, upload cells, the
  pass, readback).
- Their headline line: '10,559 of 100,000 documents match. This program: 13 ms on the GPU. Native
  RegExp: 21 ms.'
- The baseline is **native JS RegExp on one thread in a worker** — not Elasticsearch, not Lucene.

| Search's shader, 8 / 16 / 32 states | loop-emit (f32) | bit-emit (bit-packed) |
|---|---|---|
| Statements in the shader | 503 / 1,375 / 4,655 | 147 / 171 / 219 |
| 100k docs, back-to-back searches | 7.8 / 69 / 945 ms | 4.6 / 4.7 / 5.2 ms |
| Same after a 200 ms pause (a keystroke) | 16.5 / 70 / 948 ms | 11.3 / 11.9 / 13.2 ms |

Why it is fast — the engineering, all of which is index-thinking applied to a scan:

- **Documents are uniform and fixed-size** (512 cells each). That is what makes one GPU pass over
  all of them trivial. Real-world JSON is not uniform.
- **No per-query shader compile**: three program sizes (patterns up to 8, 16, 32 states) are
  compiled at load. A keystroke only turns the pattern into numeric tables (1,097 / 4,177 / 16,481
  cells) — 0.03 to 0.1 ms of JS.
- **Documents never read each other's values**, so one pass runs all 100,000 in parallel.
- **A precomputed derived column**: the highlighter runs once at load and stores a display kind per
  character (key, string, number, error...). The searcher reads it to restrict matching to keys or
  to string values. That is materialized derived data — a small index by another name.
- **Bit packing**: bit-emit proves which values are integers and which are bits and rewrites gate
  table reads as one AND of two 32-bit words (1,024 transition cells packed into 272 words). This
  is the classic inverted-index trick (compress the postings) applied to a transition matrix.

Honest caveats in their own writeup: a GPU round trip alone costs 0.6 ms or more, so the GPU is
useless for one document; a default device binds 128 MiB per buffer (65,408 documents); narrow
windows get 20,000 documents instead, and small-buffer GPUs get fewer.

## Does it beat Elasticsearch?

**No — it is a different category, and the demo never claims that.** Its baseline is single-threaded
JS RegExp. But the underlying question is worth answering precisely, because the crossover is real.

### For the demo's own workload, Elasticsearch is the wrong tool (so 'beating it' is vacuous)

100,000 documents of 300 characters is about 30 MB of text, single user, one machine, queries of the
form 'which documents contain this regex', no ranking. At that size, **ripgrep or DuckDB or Postgres
would also be fine** — the GPU is an impressive flex, but it is not the thing that makes ES
inappropriate. No index is needed for a 30 MB corpus; an index is a 30 MB corpus's problem, not its
solution.

### Where Elasticsearch wins decisively

| Dimension | The demo | Elasticsearch |
|---|---|---|
| Scale | linear memory cost: 100M such docs would need ~410 GB of GPU memory | shards across nodes; reads only the postings for the query's terms |
| Concurrency | one pass occupies the whole GPU | multiplexes many concurrent clients |
| Updates | documents uploaded once per page load; no insert/update/delete path; nothing persists | real-time indexing, deletes, replication, durable storage |
| Relevance | a match count per document and character marks — a filter, not ranking | BM25, analyzers, phrases, proximity, fuzzy, boosting |
| Aggregation | none | facets, aggregations, sorting, pagination |
| Operations | a browser tab | auth, TLS, backups, monitoring, an ecosystem |
| Ranked retrieval | absent by design | the entire point |

The functional gap that matters most: **'which documents contain this regex' is not 'which documents
are most relevant to this question'.** The demo has no notion of relevance, so it is competing with
grep, not with a search engine.

### One genuine advantage worth naming

**Deterministic cost.** Their program cannot blow up on a catastrophic-backtracking pattern like
(([a-z]+)+)+x — the GPU step takes the same time whatever the pattern. Native JS RegExp can backtrack
for minutes, and a running RegExp call cannot be interrupted, only its worker killed; the page gives
it 10 s and then prints 'more than 10 s'. Lucene largely uses automata rather than backtracking, so
this is not a like-for-like dig at ES, but predictable worst-case latency is a real property.

## The actual lesson: index vs brute force is a crossover, not a principle

Brute force is chronically underrated because hardware is absurdly fast and an index is a permanent
tax: build cost, memory, staleness, maintenance, and a whole system to operate. The demo is one more
data point for the same insight as:

- ripgrep beating a search index for occasional queries on a codebase;
- DuckDB scanning parquet faster than a warehouse for medium data;
- brute-force vector search being fine below roughly a million embeddings, with ANN only paying later;
- and the 'just give the agent a filesystem' harness-centric branch of agentic search.

Conditions under which brute force genuinely wins:

1. the corpus fits in memory (here: 410 MB for 100k tiny documents);
2. one or few concurrent users;
3. mostly static data — no need for incremental updates;
4. the query is a filter or pattern match, with no ranking requirement;
5. records are uniform in size and shape;
6. the hardware is parallel, and the batch is big enough to amortize the round trip (0.6 ms here).

Break any of those and the index comes back: corpus larger than memory, many concurrent queries,
ranked relevance required, high update rate, persistence and replication needed.

## Where this sits relative to the earlier threads

- **Against CocoIndex's premise**: CocoIndex bets that maintaining an index incrementally is worth
  the machinery. This demo delineates where that bet stops paying — single-user, static,
  memory-resident, filter-shaped queries. Worth remembering before reaching for an index.
- **But not an anti-index argument in one respect**: the demo still precomputes and stores derived
  data once (the per-character display kinds) and queries against it. The instinct is the same; only
  the persistence and the term-to-document mapping are missing.
- **For agentic search**: it is the harness-centric pattern's 'dumb filesystem tool' taken to its
  hardware limit. For local, static corpora an agent can brute-force rather than query a search
  service.
- **The transferable trick for a real pipeline**: fixed-size uniform records plus precompiled
  fixed-shape programs. Both are about removing per-query work — which is also what an index does.

---

## Addendum: could you scale this on GPU clusters (Modal / RunPod) and beat an Elasticsearch cluster?

Short answer: **not for text/regex search, and not at high query rates — but yes for dense vector
search, and yes for a corpus that fits in one node.** The reasons are physical, not a matter of
engineering effort. Numbers below are order-of-magnitude arithmetic from the demo's own
measurements, labelled as such.

### Constraint 1: a brute-force query reads the whole corpus, every time

The demo's advantage is that all 410 MB sit in one GPU's memory and one pass covers all documents.
Scale that up and the cost per query is proportional to the corpus, not to the answer:

- Demo layout: 513 float32 cells + one 4-byte kind per character = **2,052 bytes for a 300-character
  document, about 6.8 bytes per character**. A production layout (uint8 characters plus bit-packed
  kinds) is about 1.3 bytes per character — a 5x fix, but a rewrite.
- Optimized: 100M short docs = **39 GB**; 1B docs = **390 GB**.
- One H100 has 3.35 TB/s of HBM bandwidth, so 39 GB is **12 ms of pure bandwidth**. But the demo runs
  **6 to 17 times slower than its own bandwidth floor** (their 410 MB at 13 ms vs ~0.75 ms of raw
  read), because a character automaton is branchy table lookups, not dense arithmetic.
- So realistically: **~70-200 ms per query for 100M documents on one H100; ~0.7-2 s for 1B.**
- An inverted index reads the postings for the query's terms — kilobytes to megabytes — and doc
  values for the top-k. That is **five or more orders of magnitude less I/O per query**, and the
  difference does not shrink with faster hardware; it is the difference between selectivity and its
  absence.
- Corollary: brute force gets **no caching benefit**. Every query re-reads everything. An index makes
  each query touch a working set that stays hot.

### Constraint 2: the network fan-out kills the 'cluster' part

- Sharding across serverless GPU instances means either each shard holds its slice in *its* VRAM
  (aggregate VRAM >= corpus, and every query occupies *every* GPU), or you stream corpus slices over
  the network. Inter-node links run at tens of GB/s, so 390 GB over the network is **8-40 seconds per
  query**. Fatal.
- The only sane unit is therefore **one node**: 8xH100 = 640 GB of VRAM in one NVLink domain, which
  would hold roughly 0.5-1.5B short documents. That is a fixed-size box, not an elastic fleet.
- An index inverts this: cross-node traffic is proportional to postings (MBs), which is exactly why
  Elasticsearch scatter-gather works across cheap nodes at all.

### Constraint 3: cost per query-per-second goes the wrong way

- Brute force: **cost scales with corpus x QPS.** One full pass occupies every GPU for ~100 ms, so an
  8xH100 node serves roughly **10 QPS** at the 1B-document scale, with each query using the whole
  node.
- An index: **cost scales with storage plus (results x QPS)**. The same 8 nodes serve hundreds to
  thousands of QPS, and caches help further.
- Rough list prices: H100-class instances run about **$2-4 per GPU-hour**, so 8 GPUs is about
  **$12-25k per month** for ~10 QPS. A comparable Elasticsearch cluster (8 data nodes) is roughly
  $3-8k per month self-managed and serves far more QPS. At scale, brute force costs more *and* does
  less.

### Where it genuinely wins

1. **Dense vector search, not regex.** A vector scan is a dense matmul, which is the one thing GPUs
   are perfect at — nothing like the branchy automaton above. 1B vectors at 128 int8 dimensions is
   ~128 GB, and the per-query work is ~256 GFLOP, i.e. **well under a millisecond of compute** on
   modern int8 peaks, against ~116 ms for a comparable regex scan. If you want to bet on GPU brute
   force, bet here, not on text patterns.
2. **The corpus fits in one node's VRAM** (tens of millions of short documents, or ~1B quantized
   vectors). Then there is no index to build, no cluster to run, latency is single-digit
   milliseconds, and **Modal's GPU memory snapshots** (checkpoint/restore of VRAM state, sub-second
   startup) remove the cold-start objection — you can scale to zero and resume fast. For an internal
   tool, a personal RAG, or a team search box, this can genuinely beat operating a cluster.
3. **Batch, offline, and low-QPS work**: a nightly scan of a 1 TB corpus beats maintaining an index.
   This is the ripgrep argument at scale.
4. **Model-in-the-loop scoring** — a cross-encoder or an LLM judging candidates — which an inverted
   index cannot do at all.
5. **Predictable worst-case cost**: no backtracking blowups, no query-planner surprises.

### What this means for serverless GPU platforms specifically

Modal and RunPod are not the obstacle: Modal now snapshots GPU memory for sub-second resumes, which
is exactly what a resident-corpus scan needs. The obstacles are the three constraints above —
per-query full read, network fan-out, and QPS economics. Serverless also fights update freshness:
change a document and you re-upload or re-snapshot the shard, whereas an index handles incremental
updates natively.

### Precedent: regex search at scale keeps converging on indexes

- **livegrep** serves interactive regex search with a **suffix-array index costing 3-5x the indexed
  text in space**, memory-mapped so it can exceed RAM — because scanning did not cut it for
  interactive regex even over code-sized corpora. It verifies matches with RE2, which (like the GPU
  program, and unlike JS RegExp) cannot backtrack.
- The n-gram family — Google Code Search, Zoekt, GitHub's code search — all index trigrams.
- **turbopuffer**, the object-storage search engine at 100B+ vectors: hierarchical-clustering ANN as
  its primary index, plus inverted indexes for attributes and full-text, with the economics coming
  from object storage and tiered NVMe/memory caches. Its v3 redesign is about rebalancing *which*
  index is primary — not about replacing indexes with scans.
- Elasticsearch, Solr, and Lucene: inverted index plus doc values.

Every one of these had the option of scanning. They index because a query should cost what the answer
costs, not what the corpus costs.

### The architecture that actually wins

Do both, in order: a cheap **index** (inverted or ANN) for candidate generation — cacheable,
network-light, selective — then a **GPU** for the expensive part: reranking, cross-encoder scoring,
or embedding-based matching over a few hundred to a few thousand candidates. That is what Vespa,
ColBERT-style late interaction, cross-encoder rerankers, and modern vector databases do in
production. Nobody brute-forces a billion documents per query.

**Verdict.** GPU clusters running brute-force scans would be a winner for dense vector search over a
corpus that fits in one node, especially with snapshot-based serverless GPUs and low QPS. As a
replacement for an Elasticsearch cluster for text or regex search at scale, it loses on three
independent axes at once: per-query I/O, network fan-out, and cost per QPS.

---

## Addendum 2: who actually ships GPU search today?

*Verified from NVIDIA's cuVS integrations page (docs.nvidia.com/cuvs/getting-started/integrations.md),
plus vendor docs for Elasticsearch, Qdrant, LanceDB, Vespa and Milvus. NVIDIA maintains this list, so
it is close to a census of the GPU-search ecosystem.*

### The punchline

**GPU acceleration in search is real, widespread, and has mostly been applied to INDEX BUILDING and
to RANKING — not to brute-force scanning at query time.** The sharpest illustration: **Elasticsearch
itself now ships GPU acceleration, using NVIDIA cuVS to accelerate dense-vector HNSW index
construction** for ingestion-heavy workloads where CPU graph construction is the bottleneck. Elastic
builds the graph on the GPU and then converts it back for Elasticsearch's own search path. Solr and
Lucene do the same (Lucene has a cuVS-backed KnnVectorFormat; Solr's dense vector search can use
cuVS).

So the incumbents' answer to 'could GPUs beat us' was not to replace the index — it was to keep the
index and put the GPU on the part that is one-shot, embarrassingly parallel and compute-bound.

### Databases and search engines using cuVS

| Product | What the GPU does |
|---|---|
| Elasticsearch | dense-vector HNSW **index construction** (ingestion-heavy workloads) |
| Amazon OpenSearch Service | managed GPU acceleration for vector **indexing and force-merge** on 3.1+ domains; offloads Faiss index builds |
| OpenSearch | vector engine, GPU-accelerated paths |
| Apache Solr | dense vector search can use cuVS |
| Lucene (nvidia/cuvs-lucene) | a cuVS-backed KnnVectorFormat so Java search apps reach cuVS through Lucene |
| Milvus | GPU **index types** for high-throughput, high-recall vector search (query-time GPU) |
| Kinetica | GPU-native database: native vector columns, SQL vector operators, Python API |
| Oracle AI Database 26ai | AI Vector Search with cuVS |
| KX / KDB.AI | vector database using cuVS CAGRA |
| CyborgDB | encrypted vector database proxy; cuVS PoC for encrypted index build and retrieval |
| Faiss (library) | cuVS as optional backend for GPU vector indexes |
| KIOXIA AiSAQ (library) | SSD-oriented DiskANN/Vamana ANN |
| Qdrant | GPU-accelerated **HNSW indexing** since v1.13 (self-hosted and cloud); search stays CPU |
| LanceDB | GPU **IVF and HNSW index builds** via accelerator cuda/mps; claims billions of rows in under four hours on a 1-8 GPU cluster; Enterprise-only automatic GPU indexing; 'GPU acceleration changes how the vector index is built, not the lifecycle after the build' |

Also listed as data platforms integrating for vector workloads: WEKA, MinIO, DDN, Dell, NetApp,
Pure Storage (Everpure), Cloudian — storage vendors bringing GPU-accelerated vector work next to
object storage.

### Outside the cuVS list

- **NVIDIA cuVS itself** is the direct product: 'a GPU-accelerated library for vector search on the
  GPU', covering **approximate AND exact nearest neighbours**, vector compression and clustering, and
  explicitly intended to be used directly or through integrated databases. Exact NN is brute force —
  so **GPU brute-force vector search already exists as a shipped, widely integrated product**. What
  does not exist is a GPU brute-force *full-text* search service.
- **Vespa** supports GPU containers for **ONNX model evaluation in ranking** — GPU at query time, in
  the rerank/scoring stage rather than in retrieval.
- **HEAVY.AI / HeavyDB** is a GPU-accelerated SQL analytics database (docs now hosted under
  docs.nvidia.com) that 'returns SQL query results in milliseconds, even on tables with billions of
  rows' — GPU at query time, for analytics-shaped work.
- **RAPIDS cuDF / NVIDIA Morpheus** do GPU string and regex processing over dataframes and security
  telemetry — the closest production analogue of the JSON-search demo's workload.
- **Turbopuffer** (100B+ vectors, object storage, extremely cheap) is index-based on CPU with
  tiered NVMe/memory caches; its redesign is about which index is primary, not about scanning.

### What the landscape means

1. **The GPU's proven role in search is: build the index, and score/rerank the shortlist.** Both are
   compute-bound and parallel with no per-query selectivity requirement.
2. **Query-time GPU retrieval exists** (Milvus GPU indexes, Kinetica, HeavyDB, Faiss/cuVS used
   directly) but is consistently aimed at vectors, small-to-medium collections, high recall, or
   analytics — never at web-scale full-text/regex scanning.
3. **Nobody shipped 'GPU scan instead of an inverted index' for text** — and the reason is the one
   from addendum 1: a query should cost what the answer costs, not what the corpus costs.
4. The natural place for a GPU-scan search engine is therefore a niche: a corpus resident in one
   node's VRAM, low QPS, vectors or model-based scoring, static data. That niche is real (and the
   JSON-search demo is a lovely demonstration of its upper bound), but it is not the mainstream
   search market.
