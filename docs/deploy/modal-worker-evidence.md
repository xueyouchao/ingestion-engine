# Modal GPU worker: measured evidence

The measured half of [ADR-0009](../adr/0009-command-workers-and-image-only-validation.md)'s validation
gates. Four Workers were rented on Modal on 2026-10-18: one read a page directly, one served a one-page
pipeline, and two processed the pinned NOAA Corpus cold and then warm. Almost every number below was
read back from an artifact one of those runs wrote;
[Evidence caveats](#evidence-caveats) lists the few that were not.

What this file is **not**: a production acceptance, and not evidence of OCR quality. The
[runbook](modal-worker-runbook.md) remains the procedure; this is what happened when it was run, and
where the run and the procedure disagreed.

How to read it: **observed** means a file below was opened and the number read out of it, or the number
is a count recomputed from one. **Not verified** means nobody can say yet -- see
[What remains unverified](#what-remains-unverified). The one place a number's artifact is missing is
called out where it happens, not smoothed over.

No tunnel URL and no endpoint URL from any Lease appears in this file. The leased address is the
credential (ADR-0009), and the Engine Identity deliberately carries the weights revision and the image
digest instead of it. Source URLs of the corpus are fine to name and are in
[`docs/demo/pdf-demo-corpus.md`](../demo/pdf-demo-corpus.md).

The artifacts are ephemeral scratch directories under `/tmp` (`finepdfs-gpu-smoke`,
`finepdfs-pipeline-smoke`, `finepdfs-full-gpu`). They are not committed: no PDF bytes, no extracted text,
no weights and no secrets live in this repository. A quote here is all that survives of them, which is
why the quotes are exact.

## Environment

| What | Value |
| --- | --- |
| Vendor / provider | Modal, through `command` (`deploy/modal/start.py`, `deploy/modal/stop.py`) |
| GPU | `L4` (`FINEPDFS_GPU_TYPE=L4`) on every one of the four Workers |
| Worker lifetime cap | 900 s per Sandbox (`FINEPDFS_GPU_WORKER_LIFETIME=900`) |
| Readiness timeout | 600 s (`FINEPDFS_GPU_READY_TIMEOUT=600`) |
| Engine | `paddleocr-vl` -- PaddleOCR-VL-1.6 behind the pinned vLLM OpenAI server of `deploy/modal/image.py` |
| Corpus | the pinned `CC-MAIN-2026-39` NOAA Corpus, 19 Crawl Records / 18 Documents, plus the one-page scan alone |
| Driver | one CPU-only host; no local GPU, and nothing in `src/` imported Modal |

Four Workers, one per run, each with its own Lease file:

| # | Run | Observed window (UTC) | Observed wall time | Worker |
| --- | --- | --- | ---: | --- |
| 1 | direct worker, one page, no pipeline | 18:09:49 -- 18:17:19 | 450 s | one Sandbox |
| 2 | one-page pipeline | 18:18:02 -- 18:20:09 | 126.5 s | one Sandbox |
| 3 | full Corpus, cold | 18:20:43 -- 18:23:30 | 167 s | one Sandbox |
| 4 | full Corpus, warm | 18:23:31 -- 18:26:04 | 153.0 s | one Sandbox |

Observed: every window above is a start-to-stop interval read from the run's own timestamps, and every
one is well inside the 900 s cap. All four Workers were given back and their Lease files removed.

## 1. The direct worker serves PaddleOCR-VL

Purpose: the worker itself, with no pipeline in between. `start` printed one endpoint JSON object, the
tunnel was queried, one page was read through `PaddleOcrVlEngine`, then `stop` twice.

Observed from the saved answers:

* `GET /v1/models` (`/v1` is part of the leased base URL) answered one model:
  `"id": "PaddleOCR-VL-1.6"`, `"root": "PaddlePaddle/PaddleOCR-VL-1.6"`, `"owned_by": "vllm"`,
  `"max_model_len": 131072`. The pinned weights are the ones being served, under the served name the
  image pins.
* One page of `20090827I1droplog.pdf` (`459716fc0351dd1ab374eda66f152bc32116387e95c7a6437ad7382c6844db52`,
  834,848 bytes, the genuine one-page image-only AOML scan) produced **1,218 characters**, non-empty.
* The Engine Identity the engine reported for that read:

  ```text
  paddleocr-vl@paddleocr-vl@1.6:weights-c5630abae1d9:image-6ee133356d49@dpi200
  ```

  `weights-c5630abae1d9` is the pinned weights revision, `image-6ee133356d49` the image digest and
  `@dpi200` the render DPI: no `://`, no host, no Sandbox id. That is the shape ADR-0009 requires of the
  memo-bearing detail, checked against a real worker rather than a fake one. The two digests are the
  pins in `deploy/gpu/contract.py`, echoed by the Worker that ran; they are not a registry read-back, so
  they say which image was asked for, not that the tag still resolves to it today.
* `stop` printed `terminated Modal worker sb-…` for the Sandbox its Lease named, and the second `stop`
  printed `no Modal worker is recorded at <lease file>; nothing to stop` and exited 0, so stop is
  idempotent against a real Lease and not only against the fake in the tests.
* The Lease directory was empty afterwards. `start` wrote nothing to stderr.
* The 450 s covers start, readiness, one page read and stop, and this was the **first** start of the
  image, so it includes the first download of the pinned weights into the `finepdfs-hf-cache` Volume.
  Readiness on its own was not timed; it is only bounded by the runs in which it happened.

## 2. One-page pipeline: routing, one call, one published page

Same page, but through `finepdfs run --engine paddleocr-vl --model PaddleOCR-VL-1.6 --gpu-provider
command`, so the provider rented and released the Worker.

Observed:

* the run exited 0 with an empty stderr;
* `out/last-run.json` recorded `18:18:02Z` to `18:20:09Z`, **126.534 s** -- one Worker, start to stop
  included, against 450 s for the first, weight-downloading start;
* one Document, routed to OCR, **1 extraction call**, **1 Published Document** with **1 Page Record**,
  accepted text **474 characters**, and the same Engine Identity as in section 1 (reported for this run,
  and independently confirmed by the full-Corpus record for the same digest below).

The one-page run's own `out/run-summary.json`, `out/records/` and `out/search.db` are no longer present
in its evidence directory (only `out/last-run.json`, an empty `run.err` and an empty Lease directory
survived), so the four counters above were reported for that run but could not be re-read from its own
summary. They are corroborated independently for the same bytes by the full-Corpus run in section 4,
which declares one page, one accepted page, 474 characters and the same Engine Identity for
`459716fc…`, and by its routing reason (`only 0% of pages carry text, below the 50% floor`). See
[Evidence caveats](#evidence-caveats).

### The direct read and the pipeline read do not agree, and that is the honest part

Two separate OCR calls saw the same page image and returned different answers:

| | Direct call | Accepted pipeline text |
| --- | ---: | ---: |
| Characters | 1,218 | 474 |
| Printed form header, storm/scientist line, `Page 1 of 1` | yes | yes |
| `Flight ID` / `Mission ID` lines | yes | yes |
| The table's column-header line (`Drop # Sonde ID # …`) | yes | yes |
| The **14 numeric drop rows** | yes | **no** |
| Trailing `Comments` block | yes | labels only, no numbers |

Both answers keep every printed heading; the accepted text loses the table's rows and the comment
numbers. The publish path stores the engine's Page Text verbatim -- cleaning runs upstream of it and no
step drops lines by shape -- so the 474 characters are what the second call returned, not something the
pipeline removed. Whether that is sampling variance between two calls or a limit of whole-page OCR on a
sparse table is **not determined**: there is exactly one pipeline sample of this page.

So this run is evidence that the Worker serves, that the provider lends it to a pipeline, that routing
sends the image-only Document to it, and that one accepted page came back. It is **not** evidence about
OCR quality in either direction, and it is not a regression against the 342-character Docling read in
the [demo runbook](../demo/pdf-demo-runbook.md#routing-and-the-real-ocr-page-evidence): a different
engine is read on its own terms (ADR-0009).

## 3. The two-page scan: the page-mapping gate

`20090826I2droplog.pdf`, digest `b5f7ac05c298f1a0db0fff96325b0ab51fb5050de0b03f65001c2c2a3a20265f`,
read by the same worker in the full-Corpus run (section 4). Observed from its Page Records:

* `page_count` **2**, `accepted_pages` **2**, `dropped_pages` 0, `character_count` **2,259**
  (1,784 + 475, exactly);
* **page 1**: 1,784 characters, opening on `Flight ID 09082612 Storm Danny Dropsonde Scientist` and the
  lead-project-scientist narrative; it does not contain the printed form header;
* **page 2**: 475 characters, opening on
  `N42/3RF HRD GPS Dropwindsonde Scientist Log (Revised 5/2002)`; the printed header belongs to this
  page;
* the two pages share **no** text line, so no page was collapsed onto another and none was duplicated;
* both records carry the original 1-based `page_number` (1 and 2) beside `doc_id`, `digest`,
  `source_url` and the Engine Identity;
* a Search Hit on `Danny` returns page 2 as its own Chunk (section 5).

That is ADR-0009's page-mapping gate: OCR ran, pages survived, and original numbering was preserved.

## 4. The full NOAA Corpus: cold, then warm

The same `finepdfs run --corpus <pinned corpus> --out <out> --search --engine paddleocr-vl --model
PaddleOCR-VL-1.6 --gpu-provider command`, run twice with a fresh Worker each time. Both summaries are
`out/run-summary.json` copies taken at the end of their phase, and they are identical apart from
`extraction.calls` and `extraction.reused`.

| Declared counter | Cold | Warm |
| --- | ---: | ---: |
| `corpus.crawl_records` / `corpus.documents` | 19 / 18 | 19 / 18 |
| `routing.native` / `routing.ocr` | 16 / 2 | 16 / 2 |
| `extraction.requested` | 18 | 18 |
| `extraction.calls` | **17** | **0** |
| `extraction.reused` | **1** | **18** |
| `published.documents` / `published.page_records` | 17 / 313 | 17 / 313 |
| `exclusions.total` (`exact-duplicate`, `near-duplicate`) | 2 (1, 1) | 2 (1, 1) |
| `engine.identity` | `paddleocr-vl@paddleocr-vl@1.6:weights-c5630abae1d9:image-6ee133356d49@dpi200` | same |

Observed alongside:

* the cold run took **167 s** (`18:20:43Z` to `18:23:30Z`) and the warm run **153.0 s**
  (`out/last-run.json`: `18:23:31Z` to `18:26:04Z`), both under the 900 s cap;
* the routing decision for both scans was `ocr` with the reason `only 0% of pages carry text, below the
  50% floor`; the 16 native Documents were read by `pymupdf@1.28.2` and kept their text layer;
* both Exclusions are Document-level and named: an exact duplicate of `s_geography_text.pdf` and a
  near-duplicate at similarity 0.969 (`NCBOWinter202324SeasonalSummaryA.pdf`);
* one native Document, the 45-page Tribal Consultation Handbook, accepted **44 of 45** pages. A dropped
  page inside a Published Document is neither an Exclusion nor a retraction of it, and the reason for
  that page's drop is not in these artifacts;
* the warm run still rented a Worker and gave it back. That is ADR-0009's accepted cost: the Lease
  wraps the run, not the memo lookup, so a run that needs no OCR still pays for roughly a start.

### Why `cold.calls` is 17 and `warm.reused` is 18

The two numbers count different things on purpose:

* `extraction.calls` counts the memo bodies that **ran** -- one per memo key the reading pass asked for
  and did not find in the CocoIndex memo.
* `extraction.reused` counts the memo keys the reading pass **asked for** and had served from the memo.

The cold run asked for 18 keys (16 native + 2 OCR) and one of them was already memoised: the one-page
scan, read minutes earlier in section 2 under the same Engine Identity, in the memo database at
`cocoindex.db` in the working directory, which is shared across runs of this checkout. So
**17 ran + 1 reused = 18 Documents**. The warm run then asked for the same 18 keys and every one hit:
**0 calls, 18 reused**.

Which Document was the reused one is *not* recorded per Document; no artifact names it. It is inferred
from the one OCR key that could already have existed, and the 474-character accepted text of
`459716fc…` matching section 2's exactly. Treat the identity of that one reuse as an inference.

The consequence is the correction in the runbook: the old pass criterion "warm `reused` equals cold
`calls`" demands 18 = 17 and is **false for a correct cold run that is not the very first run of its
memo**. It only held in the earlier Docling rehearsal because that cold run started on an empty
database (18 calls, 0 reused, so 18 = 18). The criterion that holds in both cases is the one the runbook
now states: warm `calls` = 0 and warm `reused` = cold `calls` + cold `reused` = every Document.

## 5. Downstream: the accepted OCR text is searchable

Observed from the warm run's `out/search.db` (the Search Index the run declared, read back read-only
with SQLite so the file was not touched):

* **594 Chunks from 17 Documents** (`select count(*) from chunks`, `count(distinct doc_id)`) -- the same
  two counts the run's index step reported as `indexed 594 of 594 chunks from 17 documents`;
* the two OCR Documents contribute 4 of them (one Chunk from the one-page scan's page 1, three from the
  two-page scan's pages 1 and 2);
* the query `Danny` returns three Search Hits: page 1 of the one-page scan, **page 1 and page 2 of the
  two-page scan**. Both OCR Documents are reachable, and page 2 of the two-page scan is cited as its own
  page, not folded into page 1 -- which is what "accepted OCR text is usable evidence, not just an
  extraction result" means for this Corpus.

## 6. Teardown

Observed, per run:

* `stop` ran and reported the Sandbox it terminated (section 1 quotes the direct run's); the second
  `stop` was a no-op that exited 0;
* the Lease directory of every one of the three evidence roots was empty afterwards -- checked again
  while writing this file, not only at teardown time;
* the teardown check in the runbook (`modal container list`, `modal app list`) was run after each of the
  four Leases and found **no active `finepdfs` Worker**; the empty app record `finepdfs-gpu-worker`
  remains, which is free. This document restates those checks and did **not** re-run them: writing it
  created no Modal resource and issued no Modal command, so "no active container right now" is the
  operator's observation of 2026-10-18, not a fresh one.

Bounded but not billed: no exact bill was recorded, and the workspace's current rates were not captured
beside these runs. What is bounded is time: the four windows above sum to about 896 s of Worker life and
no single Worker could exceed 900 s, so the four Leases could not have billed for more than 3,600 s of L4
time -- four times the cap -- plus the HF cache Volume's storage. The real figure is lower and unknown.

## What remains unverified

1. **OCR quality, and the one-page table in particular.** The pipeline's accepted 474 characters for
   `459716fc…` lose the 14 numeric drop rows that the direct call returned (section 2). One sample of
   one page cannot tell a table-recognition limit from sampling variance, and no ground-truth
   transcription exists for either scan. Do not read the page counts, the character counts or the
   `Danny` Hits above as an accuracy claim.
2. **The live failure paths.** Start failure, readiness failure after the Sandbox exists, run failure
   and stop failure were **not** exercised against Modal. They are covered only offline, against
   `tests/deploy/fake_modal.py`: `test_a_readiness_failure_still_invokes_the_stop_command`,
   `test_a_worker_that_cannot_be_created_writes_no_lease`, `test_a_lease_that_cannot_be_written_terminates_the_worker`,
   `test_a_worker_with_no_tunnel_on_the_worker_port_is_reported` (`tests/deploy/test_modal_start.py`),
   `test_stop_tolerates_a_partially_started_worker`, `test_a_failed_stop_exits_nonzero_and_keeps_the_lease_for_a_retry`,
   `test_an_already_gone_worker_is_success_not_failure` (`tests/deploy/test_modal_stop.py`), and
   `test_a_command_provider_gives_the_worker_back_even_when_the_run_failed`,
   `test_a_stop_command_that_fails_is_not_discarded` (`tests/infrastructure/test_gpu_providers.py`).
   A real readiness timeout, a real run that dies mid-page and a real stop that cannot terminate are
   still unmeasured, and those are the cases where a GPU leaks.
3. **RunPod.** Nothing but the shared contract exists. No RunPod start or stop has ever run.
4. **Production cost.** Four L4 Leases over 18 Documents is a rehearsal, not a rate. The cold/warm pair
   says the warm run is not free (167 s against 153 s), and says nothing about a corpus whose OCR share
   is higher than 2 in 18.

## Evidence caveats

* **The one-page pipeline's own artifacts are missing** (`out/run-summary.json`, `out/records/`,
  `out/search.db`), although the run finished at 18:20:09Z and wrote `out/last-run.json`. Its counters
  are therefore reported rather than re-read; section 2 lists what corroborates them. Nothing in this
  file depends on those four counters being right beyond what the full-Corpus run already shows for the
  same bytes.
* **The cold run's 167 s** is a start-to-stop interval bounded by the phase's own timestamps; the
  declared `run-summary.json` deliberately carries no timing (the command's observation lives in
  `out/last-run.json`, which the warm run afterwards overwrote).
* **Readiness in isolation** was never timed. Every total above includes it.
* **Only one Sandbox id was recorded anywhere** -- the direct run's, in its own `stop` output, quoted
  truncated above. The other three Leases were released and their files removed, so "four distinct
  Workers" rests on four distinct Lease files, not on four ids.
* **The run script's own final assertion encoded the wrong criterion** (`warm.reused == cold.calls`), so
  the cold/warm pair satisfies the corrected criterion in the runbook while contradicting that line;
  the summaries above are the evidence, not the script's closing assertion.
