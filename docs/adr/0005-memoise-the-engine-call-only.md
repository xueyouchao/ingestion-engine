# 5. Memoise the engine call, and nothing else

Date: 2026-10-16

## Status

Accepted. Amended by ADR-0007: when a worker's address is not stable, the detail the provider reports
names the deployment, so the base URL among the identity's inputs below no longer has to move the memo
key. The key itself is unchanged by that amendment.

The detail the engines put there did change in the same work. The Consequences below already list page
DPI as an identity input and the PaddleOCR-VL adapter did not carry it, so both of that engine's details
now end in `@dpi{dpi}`: `model@base_url` became `model@base_url@dpi200`, and `cli:command:v1.6` became
`cli:command:v1.6@dpi200`. Every existing `paddleocr-vl` memo key therefore misses on the first run after
that lands, and the documents that engine read are read once more before being memoised again. It is a
one-time cost, not a per-run one, and the alternative -- leaving DPI out -- would serve a 300 DPI read
from a 200 DPI setting.

## Context

FinePDFs' expensive stages are the OCR engines: a document vision model reading 40 pages costs minutes
of GPU time. Everything after the engine -- cleaning, language tagging, publish rules -- is string work
that costs milliseconds and is changed far more often, because thresholds are exactly the thing an
operator tunes.

CocoIndex memoises a `@coco.fn` on the hash of its arguments and its code. Whatever sits inside that
function is invisible to the memo key: if thresholds were read from context inside the memoised
function, changing a threshold would leave a stale result in the database with no way to notice.

## Decision

The memoised unit is exactly one thing:

~~~~python
@coco.fn(memo=True)
async def extract_with_engine(digest: str, engine_name: str, engine_identity: str) -> ExtractedDocument: ...
~~~~

The memo key is therefore (content digest, engine name, engine identity), and nothing else: not the path
the file happens to live at, and not anything the engine does not control. The bytes are found under the
digest and checked against it, so a file that no longer holds what its name says fails visibly. Two
consequences, both deliberate:

* The routing decision happens *outside* the memoised function, so a threshold change that moves a
  document between "text layer" and "ocr" changes the engine name, and the memo key changes with it.
* Cleaning, language, publish rules and record writing run every time, so tuning them re-runs only the
  cheap half.

Failures raise rather than returning a failure value, so a transient engine or network failure is not
stored as a result and is retried on the next run.

## Consequences

* Re-running after a threshold change is fast: no GPU, no model calls, no re-reads.
* A document is read again when its content digest changes, when the engine name or its identity changes
  (model, base URL, page DPI, tool command or version), or when the code in that function changes.
* Moving or renaming a corpus re-reads nothing: the digest is the key and the file is looked up under it.
* A configured engine whose identity probe cannot answer (a missing executable, a failed version
  command) is an unavailable OCR engine, not a corpus-wide failure: the Documents routed to it are
  excluded one by one, the Documents another engine read still publish, and the declared run summary
  reports its identity as `unavailable` rather than a host path or an error.
* Writing different bytes into a digest-named file is a failure, not a second document and not the old
  extraction: the digest in the name, the digest in the manifest and the bytes are all checked.

## Alternatives considered

* **Memoise the whole curation of a document.** Simpler to write, but every threshold change would
  re-run the engine, which is the cost we are trying to avoid.
* **Pass the engine object as an argument** so it becomes part of the key. It is not a stable value, and
  the memo key would change whenever the transport changed identity, re-reading the whole corpus.
