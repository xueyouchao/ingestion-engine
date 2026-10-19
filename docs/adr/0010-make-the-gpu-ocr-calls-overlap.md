# 10. Make the GPU OCR calls overlap, and bound them

Date: 2026-10-19

## Status

Accepted. Amended 2026-10-19, on the day it was accepted: **the step-4 measurement has not been taken.**
`--mm-processor-cache-gb 0` and `--max-num-batched-tokens 16384` are both unchanged, and this ADR does
not claim they were revisited. Step 4 stands as a measurement still to be taken, and the
[deploy runbook](../deploy/modal-worker-runbook.md#assumptions-and-what-the-first-paid-runs-did-and-did-not-settle)
carries the check an operator runs on a real worker and what each answer would mean. What the repo owns
is that instruction, not a number it did not measure.

## Context

The rented worker serves one page per `POST /v1/chat/completions`, and today those requests are sent
strictly one after another -- not only within a document, but across the whole corpus. Measured against
the real runtime (`coco.map` -> `use_mount` -> memoised extract body, 4 vCPU, 0.5 s of blocking work per
item):

| Body shape | N=8 total | Peak in flight |
| --- | ---: | ---: |
| `async def` body doing blocking I/O -- what `PaddleOcrVlEngine` does | 4.01 s | **1** |
| plain `def` processor | 0.52 s | 8 |
| `async def` body + `asyncio.to_thread` | 0.53 s | 8 |

At N=32 (0.3 s each): `def` body 0.32 s / peak 32; `to_thread` 1.23 s / peak 32 (the default executor is
`min(32, cpu+4)` = 8 threads, so four waves); blocking async body 9.64 s / peak 1.

The cause is one line of shape. `extract_with_engine` is `async def` with nothing to await
(`flow.py:81`), and the engine it calls is synchronous `urllib` (`paddleocr_vl.py:53-60`,
`vlm_chat.py:49`). CocoIndex runs async component bodies on one shared event loop and sync bodies on a
thread each, so a blocking body freezes every sibling task. ADR-0006 describes the reading pass as
"concurrent: route + read"; for this engine it is not, and the rented GPU idles between requests.

## Decision

Fix the shape, and bound the result. Three changes, then a measurement -- and the Status says whether
the measurement was taken.

1. **A `def`, not an `async def`.** `extract_with_engine` becomes a synchronous processor. The call
   site keeps its `await`: `coco.use_mount` is `async def` in the pinned CocoIndex
   (`cocoindex/_internal/api.py`), so `read_document` awaits the mount whatever shape the processor it
   runs has. What changes is the processor's body, not the call site -- nothing was removed to go
   looking for. `OcrEngine.extract` is already sync (`ports.py:43`), so no engine or port changes and
   the core gives each document its own thread. The memo scope is unchanged -- one document, keyed on
   digest, engine name and identity -- but this is **not** free of re-reads; see "One re-read" below.
2. **Pages overlap; rendering does not.** `PaddleOcrVlEngine.extract` keeps walking its pages on the
   calling thread and hands only the HTTP request to a bounded `ThreadPoolExecutor`, so page *k*'s request
   is in flight while page *k+1* renders. Reassembly is by `PageText.number`, never completion order.
   Rendering must not move into the pool: PyMuPDF is not thread-safe, the engine renders every page
   through one open `PdfDocument` (`pdf_document.py:52`, `paddleocr_vl.py:55`), and PyMuPDF's own
   concurrency API (`apply_pages`) offers worker *processes*, not threads, and notes that `Document`
   handles do not survive a fork. Sharing that handle across threads risks a crash that takes the run
   with it, not one document's failure. A page rendered on this thread is what keeps the memo unit one
   document.
3. **One concurrency knob, at the leaf.** `FINEPDFS_ENGINE_CONCURRENCY` (default 6) bounds in-flight
   **engine calls** -- one remote page request, or one local command, for every engine that makes one --
   and nothing above them. It is deliberately *not* a permit a document body takes before waiting for its
   own pages: with N documents holding all N permits and each waiting on work that needs one, the run
   deadlocks. Every adapter that makes a call takes its permit from that one bound and holds it for the
   call alone: PaddleOCR-VL both ways, Docling's command (and its one-off version probe), and MinerU's
   command or its HTTP request. Rendering, scratch directories and parsing stay outside it.
4. **Revisit two worker pins once requests overlap.** *Not taken: see Status.*
   `--mm-processor-cache-gb 0` (`deploy/gpu/server.py:41`) makes every concurrent request re-preprocess
   its image on the worker's CPU, which saturates before the GPU does; `--max-num-batched-tokens 16384`
   (`:38`) is where a batch of page images gets split. Neither number is changed blind: measure first.

## Alternatives considered

| Option | Cost | Payoff | Verdict |
| --- | --- | --- | --- |
| **A.** Sync processor body (steps 1, 3) | ~2 lines in `flow.py`; a test that a serial pass cannot return | Concurrency = OCR-routed documents; also fixes CPU Docling, which is serial for the same reason | **Take** |
| **B.** Pages overlap, rendering stays serial (step 2) | ~15 lines in one engine; one new knob | A long scan makes the difference -- 45 pages at 2 s serial is 90 s, at 6-way ~15 s | **Take**, it costs nothing once A exists |
| **C.** `httpx.AsyncClient` + `asyncio.gather` | Rewrite `vlm_chat.py`, new dependency, async port | Same throughput as A+B on this workload; buys streaming and per-request backoff for later | Defer |
| **D.** Memo per page | New key, every existing entry invalidated (full cold re-read), revises ADR-0003/0005 | Page-level incrementality and resumability of a long document | Not now |

Rejected outright: several page images in one request (the answer interleaves and the page-to-text
mapping ADR-0009 gates on is lost); two parallel workers (doubles the start/readiness floor and the leak
surface, against ADR-0009's one-lease-per-run).

## Consequences

* **The pinned corpus does not move.** Its OCR branch is 3 pages across 2 documents, and the measured
  end-to-end baseline for a one-page run is **126.5 s** -- start, readiness, one page read and stop. That
  is an observation of a whole run, not a start-and-readiness floor: readiness was never timed on its own,
  so how much of those 126.5 s concurrency could reach is **unknown**. This change buys seconds there and
  matters only when the OCR share grows -- many scanned documents (A) or a long scan (B). It is worth
  doing because it is nearly free, not because it is the current bottleneck.
* **One re-read.** The memo key carries the memoised function's AST, not only its arguments (the memo
  canonicalizer in the pinned `cocoindex/_internal/function.py`: decorators and docstring stripped,
  then hashed). Converting `extract_with_engine` from `async def` to
  `def` changes that AST, so the first run after this change re-reads **every** Document once -- including
  the native ones, which are cheap, and the OCR branch, which is paid GPU work again. Measured on the
  pinned runtime: a comment-only edit re-runs nothing (comments are not in the AST); changing `async def`
  to `def`, or a constant inside the body, re-runs every entry. This is ADR-0005 working as intended -- a
  changed reader may not serve the old reader's text -- so it is a one-time, corpus-sized cost to budget
  with the change, not something to engineer away.
* **Rendering stays single-threaded, on purpose.** Concurrency here is I/O-bound: the calling thread
  renders a page, the pool waits on the worker. That keeps MuPDF's handle on one thread and keeps the
  knob's default a statement about the *worker*, not about 4 vCPUs of render capacity.
* **Determinism narrows.** vLLM's batching can shift a token or two even at `temperature: 0`, so "same
  bytes, same identity" no longer promises byte-identical text across a memo reset. Keys and identity are
  unaffected; the first read is still the one the memo remembers.
* **Failure is still per document.** A page that fails fails its document, not its siblings -- the same
  contract `read_document` already has -- and the run's bound carries on: the read's page requests that
  are still waiting are cancelled when a page fails, so the bound they held goes back to the other
  documents instead of being spent on one that has already failed. A request a worker has already
  started cannot be cancelled: it runs to completion and is not recalled.
* **A regression guard, not a comment.** The test uses an engine with a barrier and asserts the reading
  pass really overlaps, so the serialization cannot come back unnoticed.
