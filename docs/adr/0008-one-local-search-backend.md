# 8. One local full-text index, behind the shared search contract

Date: 2026-10-18

## Status

Accepted. Budget call for the demo, not a rejection of the proposed Elasticsearch deployment.

## Context

The demo spec proposed a pinned single-node Elasticsearch as the P2 search backend, with a cut rule:
if it cannot be made stable inside the two-day budget, index the same record contract in a local
full-text store and ship **one** backend, not two. The fallback is explicitly not a second permanent
design.

The environment was checked rather than assumed. The check is `command -v elasticsearch` plus
whatever endpoint the operator configured; on the rehearsal host (2026-10-18) neither existed, and
standing up a reliable single-node deployment (JVM, image, tuning) did not fit the remaining budget.
The corpus is 18 Documents / 325 pages, so a distributed search engine buys nothing this demo can
measure.

## Decision

The seam is the record contract, and the backend is behind it:

* `finepdfs/domain/search.py` defines `SearchQuery`, `SearchFilters`, `SearchHit` and `SearchResults`.
  A hit names the same things a page record does -- `doc_id`, source URL, PDF digest and the original
  1-based page number -- plus `chunk_id`, chunk offsets/ordinal, text, a highlighted snippet and the
  lexical score. No backend name appears in it.
* `SearchIndex` in `finepdfs/domain/ports.py` is the write/read port: idempotent `upsert` that reports
  every per-chunk failure, `retain` that deletes what is no longer declared, `search`, and the freshness
  pair `record_freshness`/`state`, where `state` reports the declared-evidence fingerprint the index
  mirrors and raises `SearchUnavailable` when it cannot be read at all.
* `finepdfs/application/search.py` owns the use cases: `reindex_search` makes the index exactly the
  declared chunk set, and `SearchAcceptedEvidence` / `search_pdfs` validate and answer a query. The
  browser and the agent call this, never a database.
* The single shipped adapter is `finepdfs/infrastructure/search/sqlite_fts.py`: SQLite FTS5 from the
  Python standard library, BM25 ranking and `snippet()` highlighting. The CLI exposes it as
  `finepdfs index-search` and `finepdfs search`.
* Chunks are declared target state written by the flow (`out/chunks/<key>.jsonl`), derived only from
  accepted Page Texts. The index is derived state rebuilt from those files; it makes no curation,
  Deduplication or Publish Policy decision of its own. There are no embeddings.

## Consequences

* Replacing the backend is an adapter behind the same port and contract; nothing in the browser or the
  agent changes. If Elasticsearch is ever stood up, it arrives as one adapter replacing this one, not
  beside it.
* The index is only as fresh as the last `index-search` after a run. That is deliberate: the flow
  remains the only writer of accepted evidence, and rebuilding is a read of declared state.
* FTS5 gives lexical BM25, metadata filters (language, Engine Identity, `doc_id`) and snippets on one
  host. It does not give Elasticsearch's analysis chains, clustering or distributed scale, and its
  score magnitude is not comparable with any other engine's. The demo makes no retrieval-quality claim
  from either.
* Every per-chunk failure is reported, and a retry is idempotent; `chunk_id` is the primary key, so a
  reindex cannot duplicate a hit, and `retain` retracts replacements, fewer pages, exclusions and
  removed Crawl Records.
* "Only as fresh as the last `index-search`" is enforced, not documented: the rebuild records a
  fingerprint of the declared chunk set, and every read compares it with the current set. A stale,
  empty, schema-less or missing index is an explicit `available: false` with the reason and "run
  `index-search`", never a shorter hit list and never stale text -- neither the explorer nor the agent
  can serve one. A partial rebuild leaves the index stale on purpose.
* Reading is read-only, by construction: the adapter opens SQLite with `mode=ro`, creates no schema and
  writes nothing, so serving the explorer (or asking a question) cannot rebuild a database or become a
  second writer. Only `index-search`, the operator's explicit rebuild, may write.
* Chunks are declared only when a run enables search (`FINEPDFS_SEARCH`, `--search`), per the spec's
  "chunks when search is enabled"; a plain curation run declares no chunk export and retracts a stale
  one. The index path is resolved in one helper (`interfaces/search_paths.py`) shared by
  `index-search`, `search`, `explore` and `ask`, honouring `FINEPDFS_SEARCH_INDEX`.

## Alternatives considered

* **Pin Elasticsearch anyway.** Cut: no stable single-node deployment inside the budget, and the
  source spec says to fall back rather than risk the demo.
* **Ship both backends and choose at runtime.** Forbidden by the spec and a maintenance fork: two
  behaviours to test, two freshness stories, and the contract no longer proves anything.
* **Add embeddings or a vector index.** Out of scope for this gate; lexical evidence and citations are
  what the demo can verify.
