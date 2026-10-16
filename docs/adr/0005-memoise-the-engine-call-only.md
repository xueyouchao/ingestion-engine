# 5. Memoise the engine call, and nothing else

Date: 2026-10-16

## Status

Accepted

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
async def extract_with_engine(pdf_path: pathlib.Path, engine_name: str) -> ExtractedDocument: ...
~~~~

The memo key is therefore (document, engine). Two consequences, both deliberate:

* The routing decision happens *outside* the memoised function, so a threshold change that moves a
  document between "text layer" and "ocr" changes the engine name, and the memo key changes with it.
* Cleaning, language, publish rules and record writing run every time, so tuning them re-runs only the
  cheap half.

Failures raise rather than returning a failure value, so a transient engine or network failure is not
stored as a result and is retried on the next run.

## Consequences

* Re-running after a threshold change is fast: no GPU, no model calls, no re-reads.
* A document is read again when its bytes change (the path is its digest), when the engine name changes,
  or when the code in that function changes.
* The engine identity (name plus version or endpoint) is not part of the key by itself: the engine's
  *name* is, and a version upgrade of PyMuPDF is caught because the adapter builds its identity from
  `pymupdf.version`, which is in the code path... but only when that function's code hash changes. The
  honest limitation is documented; a version bump that does not touch this repository does not
  invalidate memos.

## Alternatives considered

* **Memoise the whole curation of a document.** Simpler to write, but every threshold change would
  re-run the engine, which is the cost we are trying to avoid.
* **Pass the engine object as an argument** so it becomes part of the key. It is not a stable value, and
  the memo key would change whenever the transport changed identity, re-reading the whole corpus.
