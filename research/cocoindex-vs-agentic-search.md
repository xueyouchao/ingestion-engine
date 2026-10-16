# CocoIndex vs the 'agentic search: retrieval, harness, or model?' framing

*Compares CocoIndex against Doug Turnbull's article (aipoweredsearch.com / SoftwareDoug blog),
'Agentic search - retrieval, harness, or model?'*

## Short answer

**No — they are different layers of the same stack.** The article is entirely about the *query-time*
side: when an agent needs context, who is in charge of finding it? It lays out three patterns:

1. **Retrieval-centric** — build good search (lexical/vector + reranker + query understanding) and let
   search lead the agent by the nose.
2. **Harness-centric** — give the agent dumb primitives (BM25, a filesystem) and let it explore,
   with a judge giving relevance feedback and skills/query plans guiding it. Downside: token cost,
   exploration restarts fresh in every context window.
3. **Model-centric** — fine-tune an LLM to know how to search your data (SID, Glean Waldo); the
   agent memorizes what the harness taught.

CocoIndex answers a different question: **what sits underneath all three, and how does it stay
fresh, clean, and cheap?** It is the *write path* (ingest -> transform -> maintain the index), not
any of the three *query path* patterns. The article's own closing — 'converging on a fascinating
mix of easier to organize data' — is exactly the slot CocoIndex occupies (the author names PageIndex
in that bucket; CocoIndex is the open-source, incremental-engine take on it).

## Layer map

~~~
   QUERY TIME (the article's territory)          WRITE TIME (CocoIndex's territory)
   ---------------------------------------------------------------------
   Agent  <->  [ retrieval | harness | model ]   sources -> parse -> enrich/chunk -> embed
                        |                                     |        (incremental, memoized,
                        v                                     v         lineage-tracked)
                 search backend(s)  <=================  Postgres/Doris/LanceDB/Qdrant/graph
                                                           maintained as target states
~~~

## How CocoIndex relates to each pattern in the article

**Retrieval-centric.** CocoIndex maintains the retrieval backend itself: hybrid vector + full-text
indexes in Postgres/Doris/LanceDB, kept in sync with sources at delta cost (measured: warm rerun
0.30 s, zero re-execution). It improves *what* search can find (fresh, deduped, metadata-enriched,
lineage-cited chunks); it does not improve the ranking side (no reranker, no query understanding).

**Harness-centric.** The article says: give agents dumb tools and let them explore. CocoIndex's
flagship, cocoindex-code, is exactly a *harness-facing tool provider* — an MCP server exposing
search / refresh_index to a coding agent over a cheap embedded index. Two article points matter
here:
- 'Optimizing content for findability' (document the purpose of a chunk, not just the chunk) is an
  index-time transformation — an LLM-enrichment step in a CocoIndex pipeline. The reason this is
  practical at all is layered memoization: rewriting your enrichment prompt re-runs the enrichment
  step but not re-parsing; editing one document re-runs only that document's chain (measured: 20 of
  310 docs). Iterating on findability is affordable instead of a full-corpus rebuild.
- The pattern's stated downside is token cost from repeated exploration. Fresh, deduplicated,
  canonicalized content attacks that: fewer wasted tool calls. Entity resolution (CocoIndex's
  FAISS + LLM-pair-resolver op) fixes at write time what the article's 'judge' fixes at read time
  — e.g. 'ABE' resolving to 'the A/B testing tool' with canonical metadata, so the agent does not
  have to rediscover it every context window.

**Model-centric.** Not CocoIndex. It has no training loop. At most, pipelines built on it are a
plausible source of labeled traces (query -> judged-relevant chunk, with byte-level provenance) for
fine-tuning a search model — the same lineage the article would want in a training set.

## What CocoIndex does NOT do (article-shaped gaps)

- No reranker, query understanding, diversity, or ranking logic.
- No judge / relevance-feedback loop, no agent orchestration, no skills engine.
- No fine-tuning; nothing model-centric.
- It is write-path only — query-time is your app or agent hitting the target store.

## Bottom line

The article asks *who steers the agent at query time — search, a harness, or a trained model?*
CocoIndex doesn't take a side; it makes sure that whichever side wins, the index it steers over is
fresh, deduplicated, purpose-enriched, and cheap to keep that way — which is the prerequisite all
three patterns quietly assume. The failure modes the article opens with (agents making false
assumptions about domain context, distractors confusing reasoning) are partly *data* problems:
stale, duplicated, unlabeled content. Fixing those continuously at write time is CocoIndex's job;
choosing who consumes them at read time is the article's.

---

## Addendum: where do Solr, Elasticsearch, and Luxir fit?

**Yes, related — but by a specific relationship.** Solr, Elasticsearch, and Luxir are *search
engines*: infrastructure that answers 'given this query, rank these documents fast.' 'Agentic
search' (per the article) is not an engine — it is a query-time pattern about *who decides how to
search*. So agentic search always sits on top of an engine; the engine is the tool being wielded.

~~~
you ask the agent a question
   |
   |  agentic search = WHO STEERS (retrieval-centric | harness | model)   <- the article
   v
the agent issues tool calls
   |
   v
search engine = WHAT EXECUTES: Solr | Elasticsearch | Luxir               <- engines
   ^
   |  someone has to keep the index fed, fresh, deduped, enriched
   |
CocoIndex = the write path that maintains the index                      <- previous section
~~~

Per-engine notes:

- **Solr** — the classic Apache Lucene full-text engine (BM25, facets), the ancestor of this whole
  family. In the article's terms it is the battle-tested backend of *retrieval-centric* agentic
  search, and a perfectly good 'dumb tool' for a *harness-centric* agent to drive.
- **Elasticsearch** — same Lucene lineage plus aggregations; has since added dense vectors (HNSW),
  hybrid retrieval, and learned-sparse (ELSER) — i.e., it is actively converging toward what
  agentic/RAG workloads need, from the engine side.
- **Luxir** (luxir.org) — pre-1.0, Apache-2.0, from Yonik Seeley (original Solr author). Native code,
  single-node scale-up philosophy (mmap segments, work-stealing, SIMD). Hybrid by construction:
  BM25 + kNN vectors + facets + analytics in one engine, with filters applied *inside* vector
  search and lexical+vector rank fusion in a single request — exactly the hybrid recipe the
  article's retrieval-centric pattern wants, built into the engine instead of assembled from parts.
  Caveats today: pre-1.0, single-node, no auth/TLS, no replication yet.

The practical mapping to the article's three patterns: in *retrieval-centric*, Solr/ES/Luxir ARE the
'good search' you build; in *harness-centric*, they are the dumb-ish tools (a BM25 backend, a curl-able
JSON API) the agent explores with while a judge corrects it; in *model-centric*, they are still the
execution substrate the fine-tuned model calls. The engine never decides the strategy — which is why
the article and the engines are complementary rather than competing.

CocoIndex note: none of these three are built-in CocoIndex targets (its 'engine' targets are
Postgres/Doris/LanceDB/Qdrant/etc.; Doris plays the hybrid-engine role in its examples). Feeding
Luxir specifically is plausible through its NDJSON/HTTP ingest API via a custom target connector
(docs advanced_topics/custom_target_connector), with CocoIndex providing the incremental
parse-enrich-embed-dedupe upstream of it.
