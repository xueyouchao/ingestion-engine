# 6. Two kinds of duplicate, two mechanisms

Date: 2026-10-16

## Status

Accepted

## Context

FinePDFs deduplicates twice: exact duplicates on PDF bytes early, and MinHash near-duplicates on text
late. Its implementation does this with datatrove jobs run in sequence.

In an incremental pipeline the two are not the same problem. Identical bytes are knowable before any
work is done, because the corpus manifest already carries every document's digest -- a pure function of
the manifest, with no state and no ordering to worry about. Near-duplicates are only knowable after the
text exists, and the decision is inherently corpus-wide: a document is a duplicate *of another
document*, which no single document's component can see.

## Decision

* **Exact duplicates are suppressed inside the flow.** `exact_duplicate_keepers` picks, for each digest,
  the smallest URL as the keeper -- order-independent, so two runs agree. Non-keepers publish an
  exclusion naming the keeper, and publish no text at all.
* **Near-duplicates are a corpus-wide pass**, `finepdfs dedupe`, over the published records and texts. It
  is deterministic (documents are compared in URL order, the keeper is the smallest URL), it rewrites
  the affected records with `dedup_status` and `duplicate_of`, deletes the duplicate's Markdown, and
  writes `out/dedupe/{kept,duplicates}.jsonl`.

## Consequences

* Exact duplicates cost nothing and never appear in the incremental state.
* The dedup pass is idempotent and cheap (text fingerprints over files, no model calls), but it is a
  step an operator must run after `run`; re-running `run` re-publishes candidates and the pass must be
  run again. The README says so.
* A duplicate is reported, not silently dropped: the exclusion and the dedup report both name which
  document won, which is what made it possible to verify on real data.

## Alternatives considered

* **A shared mutable dedup index consulted during the flow.** Which document wins would depend on which
  component ran first, making output non-deterministic and the memoised state order-dependent.
* **A corpus-level component in the flow.** CocoIndex's model is per-item components; a component that
  sees every other document's text is not expressible, and pretending otherwise would have hidden the
  limitation rather than documenting it.
