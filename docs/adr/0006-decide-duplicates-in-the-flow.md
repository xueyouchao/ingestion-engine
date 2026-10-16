# 6. Decide both kinds of duplicate in the flow, in one pass that sees the whole corpus

Date: 2026-10-16

## Status

Accepted. Supersedes an earlier version of this decision, which moved near-duplicates into a separate
`finepdfs dedupe` command.

## Context

FinePDFs deduplicates twice: exact duplicates on PDF bytes early, and MinHash near-duplicates on text
late. The two are different problems:

* Identical bytes are knowable from the corpus manifest alone, before any work is done -- the digest is
  already there, and the keeper of a digest is a pure function of the URLs.
* Near-duplicates are only knowable after the text exists, and the decision is corpus-wide: a document
  is a duplicate *of another document*.

The first version of this decision made near-duplicates a separate command that rewrote records and
deleted Markdown. The architecture review on this branch found why that was wrong: those files are
*declared* by the flow, so the next run re-declared every record as unique and recreated every deleted
Markdown file. The dedup result lasted until the next run, and the property the whole design exists to
provide -- that a run is a pure function of the corpus, and target state is the only writer -- was
false exactly where duplicates exist.

## Decision

The flow runs in two passes, and the second pass decides everything:

```text
app_main
  coco.map(read_document, requests)         concurrent: route + read, memoised per (document, engine)
  use_mount(publish_corpus, readings, ...)  one component that sees every text at once
```

`read_document` returns what it found *as a value* instead of writing anything, so the deciding pass has
every document's cleaned text in hand. `publish_corpus` then plans both kinds of duplicate -- exact from
the digests, near from the fingerprints -- and declares, per document, either its Markdown and record or
the exclusion that says why it is not published. The dedup report is declared too
(`out/dedupe/{kept,duplicates}.jsonl`), so nothing about duplicates is written out of band, and no
command has to be run in a particular order.

Two consequences worth naming:

* An exact duplicate is skipped *before* it is read. Concurrency defeats the memo here: two identical
  documents read in parallel both miss the memo and both call the engine, so the keeper check has to
  happen before the expensive work rather than after it.
* The deciding pass holds every text in memory for the length of the run. That is what a corpus-wide
  decision costs, and it is why the corpus on disk is a directory of thousands of documents rather than
  the whole crawl.

## Consequences

* `run` once: duplicates are gone. `run` again: no engine work, no changes.
* There is no `dedupe` command. `dry-run` prints what the deciding pass would decide; `run` is the only
  writer in the system.
* Changing the similarity threshold changes the declared exclusions and nothing else, because the
  threshold lives outside the memoised unit.
* A document that stops being a near-duplicate (say, its keeper left the corpus) is published again on
  the next run, because the decision is recomputed rather than remembered.

## Alternatives considered

* **A separate dedup command that rewrites the published corpus.** Tried, committed earlier on this
  branch, and reverted: it made target state a lie and forced an ordering on the operator.
* **A shared mutable dedup index consulted while reading.** Which document wins would depend on which
  component ran first, making the output non-deterministic.
