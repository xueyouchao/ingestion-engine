# Where the demo clock's facts can come from: no per-Document duration exists, and CocoIndex 1.0.25 cannot supply one

*Ticket 05, "Where the clock's facts come from". "The animation is a compressed replay of a real timeline,
and the run's most interesting shape is that 127.5 s of 133.8 s was two scans. But no per-Document
duration is recorded anywhere… Determine, and recommend one." Answered against the installed code on
this machine: the flow, the metrics accumulator, the run log, the published record shapes, the LMDB state
database of the `pymupdf` demo run in `.demo/`, and CocoIndex 1.0.25 in `.venv`. Nothing was installed,
no model was downloaded, the pipeline was not run, and no file outside this note was modified.*

## The answer, up front

**Nothing per Document is recorded today (confirmed), CocoIndex exposes only integer work counts and no
duration at any layer (confirmed, by inspection and by a probe), so the instrumentation is the app's own
job (it is possible and cheap in code but needs a fresh cold run to produce numbers), and the animation
can be truthful without it by labelling the two runbook figures as single warm-cache observations on the
two OCR cards and by pacing every other card by an explicitly-labelled remainder.**

**Recommendation: do not add per-Document timing for this page. Ship the lane-level attribution
(Option A) now, and treat the per-call sidecar (Option B) as a deferred, ~100-line change whose trigger is
ticket 15's cold re-run — because the sidecar can only be populated by re-running cold, and the runbook
states the recorded numbers are "measured then and not re-measured here"**
(`docs/demo/pdf-demo-runbook.md:130-131`).

The page's honesty then rests on three printed sentences, not on new data:

1. the run total — `133.8 s (observed)` — from `out/last-run.json`, labelled observed;
2. the two scan figures — `54.5 s` (the one-page scan) and `73.0 s` (the two-page scan) — with
   `single warm-cache observation, not a benchmark` attached **to the numbers themselves**;
3. every other card — `not timed individually` plus `the remaining ≈6.3 s is a subtraction, not a
   measurement` — and a compression label that says the non-scan cards are paced by that remainder.

One finding that changes the wording, below in §4: **the three runbook figures do not close.** 133.8 −
(54.5 + 73.0) = 6.3 s for *all* non-extraction work, yet the same work measured alone on a warm rerun is
8.6 s (`docs/demo/pdf-demo-runbook.md:137`). The remainder is therefore an upper bound that is 2.3 s
*smaller* than an independent measurement of the work it is supposed to contain, so the page must print
it as a subtraction and must not let a visitor add the cards up to 133.8 s.

## 1. What a run records per Document: nothing — confirmed

The strongest claim in the ticket survives every artifact I checked. There is exactly **one** duration in
the whole source tree, and it is the run total.

| Artifact | What it carries | Per-Document duration? |
|---|---|---|
| `out/run-summary.json` (declared, `src/finepdfs/application/metrics.py:95-135`) | `schema_version`, corpus/routing/extraction/published/exclusion **counters**, engine name+identity | **No.** `summary()` has no time field and its docstring forbids one (`metrics.py:105-111`). |
| `out/records/<key>.json` (declared, `src/finepdfs/domain/publishing.py:45-53`) | `url, digest, engine, routing_decision, routing_reason, language, page_count, accepted_pages, dropped_pages, character_count` | **No.** `DocumentRecord.as_json` is `asdict` (`publishing.py:52`); there is no field to hold one. |
| `out/pages/<key>.jsonl` (declared, `publishing.py:56-68`) | `doc_id, source_url, digest, page_number, text, engine` | **No.** |
| `out/excluded/<key>.json` (declared, `publishing.py:68-69`) | `url, key, kind, reason` | **No.** |
| `state.db/mdb` (CocoIndex memo, not published) | memoised `ExtractedDocument` values under `extract/<digest>` — `pages/number/text/engine/{name,detail}` | **No.** A `strings` scan of the demo run's `data.mdb` shows exactly those field names and no `time`/`timestamp`/`duration` key (see §Measurements). |
| `out/last-run.json` (observed, `src/finepdfs/interfaces/run_log.py:21-43`) | `observed`, `note`, `started_at`, `finished_at`, `elapsed_seconds` | **No — one run total.** `ObservedRun` has three fields and none is per Document. |
| the CLI line (`src/finepdfs/interfaces/summary.py:32-53`) | one line of counted facts + `elapsed {x}s (observed)` | **No.** |

The demo run in `.demo/` confirms the shape end to end: `.demo/out/last-run.json` is
`elapsed_seconds: 1.388` for a run whose declared summary says `extraction calls=2, reused=16` — two
engine calls, one total. Its `.demo/out/run-summary.json` carries no timing key at all.

Two structural facts make this a decision rather than an omission, and both are documented in code:

- The flow may not write observations: `run_log.py:1-9` and `metrics.py:11-13` say the observation
  belongs to the command wrapper, and `flow.py:211-213` repeats it where the summary is declared.
- CocoIndex's own statistics are explicitly *not* folded in: "CocoIndex's own component and memo
  statistics are deliberately not folded in here; they count work units, not engine calls"
  (`metrics.py:11-13`). So even the counts already in `run-summary.json` are the app's, not CocoIndex's.

**Verdict 1: confirmed — "nothing per Document" is literally true, in every declared file, in the state
database, and in the terminal output. The only duration anywhere is one run total in a non-declared
file.**

## 2. What CocoIndex 1.0.25 exposes: counts only, no duration — confirmed by inspection and probe

CocoIndex does have a public stats surface — it is exported in `cocoindex.__all__`:
`ComponentStats`, `StatsGroupHandle`, `UpdateSnapshot`, `UpdateStats`, `UpdateStatus`, `stats_group`,
`show_progress`, plus `UpdateHandle.stats()/watch()`. That surface turns out to be **integer counters
only**.

| Exposed thing | Where | What it actually contains |
|---|---|---|
| `ComponentStats` | `.venv/.../cocoindex/_internal/update_stats.py:54-76` | six `int`s: `num_execution_starts, num_unchanged, num_adds, num_deletes, num_reprocesses, num_errors` (plus derived int properties). **No float, no timestamp.** Docstring: "Per-processor stats group, mirroring Rust's ProcessingStatsGroup." |
| `UpdateStats.by_component` | `update_stats.py:79-103` | `dict[str, ComponentStats]` keyed by **processor name**, not by component path or digest. |
| `UpdateHandle.stats()` / `watch()` | `.venv/.../cocoindex/_internal/app.py` (class `UpdateHandle`) | A snapshot of the same counters; `watch()` yields `RUNNING`/`READY` snapshots for a progress display. |
| `coco.stats_group(title)` | `.venv/.../cocoindex/_internal/component_ctx.py:453-490` | "Aggregate the stats of everything mounted within this scope separately" — a *grouping* of the same counters, "mirrors `UpdateHandle`'s `stats()`. **No time is aggregated.** |
| the core boundary itself | `.venv/.../cocoindex/_internal/core.pyi:294, 301, 307` | `stats_snapshot() -> tuple[int, bool, dict[str, dict[str, int]]]`: a version int, a ready bool, per-processor dicts of **ints**. There is no float in the stats channel at all, so no duration can be hiding behind the Python layer. |

A grep of the entire installed package for `duration|elapsed|perf_counter|start_time|end_time|timestamp`
returns nothing in the stats or function-execution path — only `timeout`/`deadline` machinery
(`DeadlineContext`, `remaining_secs`) which measures a *limit*, not an observation, and `time.time()` in a
Doris target's temp-table name. The CLI's progress output (`report_to_stdout=True`, used at
`src/finepdfs/interfaces/cli.py:629` and `src/finepdfs/interfaces/demo.py:199`) is rendered from those same
integer counters; it reports work counts, not seconds.

**A probe, to check the shape rather than trust the docstrings.** A toy app — one memoised fn, three items,
one `stats_group`, no PDF, no network, its own temp DB — was run against the installed package:

```text
group handle type: StatsGroupHandle
UPDATE STATS: UpdateStats(by_component={'main': ComponentStats(num_execution_starts=1,
  num_unchanged=0, num_adds=1, num_deletes=0, num_reprocesses=0, num_errors=0)})
by_component keys: ['main']
ComponentStats fields: ('num_execution_starts','num_unchanged','num_adds','num_deletes',
  'num_reprocesses','num_errors')
```

Three work items, one `by_component` entry, six integer fields. **The finest granularity CocoIndex offers
is the processor, not the work item** — so even the *counts* are not per Document without the app keeping
its own keys, which is exactly why `RunMetrics` keys on `(digest, engine_name, engine_identity)`
(`metrics.py:26, 67-73`) instead of reading CocoIndex.

Labelling of the API, as the ticket asks: `stats_group`/`ComponentStats`/`UpdateHandle.stats()` are
**exported public API with docstrings**, but the installed wheel ships no API reference to document them
(`cocoindex-1.0.25.dist-info/METADATA` is a 442-line project README with no `stats_group`, `ComponentStats`
or timing section), and CocoIndex's own docs were not fetched here (no network). So: *public-but-thinly-
documented*, and — the decisive point — **counts-only at both the documented and the core boundary, so no
labelling rescues per-Document timing from it.**

**Verdict 2: refuted — CocoIndex 1.0.25 exposes per-processor counts and no duration anywhere, documented
or undocumented. The app must time its own calls.**

## 3. What one observed duration per engine call would cost

### Where it can be taken, and why only there

There is exactly one seam that knows "an actual engine call happened": the memoised body
`extract_with_engine` (`src/finepdfs/infrastructure/cocoindex/flow.py:80-100`). It already says so in a
comment at `flow.py:97-99` — "This body runs only on a memo miss, so this is one actual Extraction" — and
it already records the key into `RunMetrics.count_extraction` (`flow.py:99`). A `perf_counter()` pair
around `runtime.extraction.extract_with(...)` (`flow.py:100`) is the whole measurement.

Nothing lower should own it: the engine adapters (`src/finepdfs/infrastructure/engines/docling_engine.py:59`,
`pymupdf_engine.py:27`, signature at `src/finepdfs/domain/ports.py:43`) have no run context, and the
command wrapper (`cli.py:627-630`, `demo.py:198-200`) is outside CocoIndex and cannot see individual calls.
Putting the timer in an adapter would also put a wall clock in the infrastructure of the *declared* path,
one step from `DocumentRecord` — the exact place §3's leak test is designed to fence off.

### Where it must be written, and why not from the flow

The flow must not write it. `run_log.py:1-9` establishes the split, `flow.py:211-213` repeats it at the
summary declaration, and `metrics.py:11-13` says there is deliberately "no second writer beside" the
summary. So the recording is two-part, mirroring `RunMetrics` exactly:

1. **Accumulate in memory** on a sibling of `RunMetrics` carried by the `Runtime` (which already holds
   `metrics: RunMetrics = field(compare=False, repr=False)`, `src/finepdfs/infrastructure/cocoindex/runtime.py:57`),
   keyed `(digest, engine_name, engine_identity)` — *the memo key of `flow.py:81`*, so the sidecar and the
   memo answer the same question.
2. **Write from the command wrapper** — `conclude_run` (`src/finepdfs/interfaces/summary.py:17-29`) already
   stops the clock and writes `last-run.json`; a sibling there writes `out/engine-timings.json` beside it,
   using the `OBSERVED_RUN_LOG`-style constant pattern of `run_log.py:21`. Both wrappers already funnel
   through `conclude_run` (`cli.py:630`, `demo.py:200`), so there is one writer for both commands. Both hold
   the `runtime` (`cli.py:617`, `demo.py` builds or is given one), so passing its timings to `conclude_run`
   is a signature change of one function.

Proposed non-declared shape (observed, never declared):

```json
{
  "observed": true,
  "note": "observed wall-clock timing of individual engine calls in one run; not declared target state, not reproducible",
  "engine_calls": [
    {"digest": "…", "engine": "docling", "identity": "docling@cli:docling:v2.140.0",
     "seconds": 54.5, "page_count": 1}
  ],
  "reused_without_call": 16
}
```

### What test would pin that it never leaks

Three tests, two of them extensions of tests that already exist:

- **Extend `tests/infrastructure/test_run_summary.py:183-200`** — the test that already runs twice over the
  same state and asserts the declared bytes are unchanged and that `last-run.json` does not exist
  ("the flow declares no timing log: observation belongs to the command wrapper"). Add: the flow-only path
  writes **no `engine-timings.json` either**, and `run-summary.json` stays byte-identical for a *third* run
  even when the sidecar differs (per-call times differ every run by construction).
- **Extend `tests/infrastructure/test_run_summary.py:203-221`** — the test asserting summaries carry no
  observation of when the run happened. Generalise its assertion from `"elapsed_seconds" not in` to also
  reject `duration`, `seconds`, `started_at`, `finished_at` in `run-summary.json`, **and read every
  `records/*.json` and assert no numeric duration field survived there** (`publishing.py:45-53` is the
  shape; this test is what stops a future refactor from parking the timer on `DocumentRecord`).
- **A new reuse test** (same fixture style as `test_run_summary.py:224-243`): cold run → sidecar has one
  entry per `extraction.calls`; warm rerun → sidecar has **zero** entries while the declared summary still
  says `calls=0, reused=18`. This pins the memo interaction as a *state*, not a `0.0 s`, the same way
  `explorer.py:129-137` already treats an absent observation as "not recorded" rather than zero.

That last test points at the real trap.

### How it interacts with the extraction memo

**A reused Extraction has no call to time, so a warm run's sidecar is empty.** Two consequences the page
must respect if Option B is ever built:

- The sidecar is a *calls* artifact, not a *documents* artifact. On the demo's own warm rerun
  (`docs/demo/pdf-demo-runbook.md:137`, `0 calls / 18 reused`) it can say nothing at all.
- The demo's `.demo/` pymupdf run already shows the asymmetry in declared form: `calls=2, reused=16`. A
  per-call sidecar for it would carry two numbers, not eighteen, and the export (ticket 09) must not
  present the missing sixteen as zeros.

One property makes the per-call numbers *stackable*, and it is a property of today's code, not a
guarantee: `ExtractionStep.extract_with` is a synchronous call (`src/finepdfs/application/curate.py:96-97`,
engines' `extract` are plain `def`), invoked from inside an `async` memoised body, so it blocks the event
loop and calls serialise — the runbook's own claim at `docs/demo/pdf-demo-runbook.md:140-143`. Per-call
wall times therefore add up to roughly the extraction wall time today. If an engine ever becomes async,
the sum would exceed the wall time while every individual number stayed true, and a page that stacks bars
into a total would start lying. Say so in the sidecar's note or in the test, not only in prose.

### Cost

| Piece | Estimate |
|---|---|
| accumulator (~20 lines) + `runtime.py:57` sibling field (1 line) | small |
| one `perf_counter` pair in `flow.py:100` + docstring | ~4 lines |
| `engine-timings.json` writer in `run_log.py` + `conclude_run` signature (`summary.py:17`) + two call sites (`cli.py:630`, `demo.py:200`) | ~25 lines |
| three tests (two extensions, one new) | ~60 lines |
| README output layout (`README.md:295-308`), runbook schema section (`:179-209`) | ~12 lines |
| **the data itself** | **one cold Docling run** — 133.8 s recorded, 148.2 s through the demo command (`runbook:136,148`), plus model availability; the runbook explicitly refuses to re-measure (`:130-131`) |
| knock-on | the exporter contract (ticket 09) grows a fourth input; the page must still decide how to draw 2 of 18 calls |

**Total: ~120 lines and one cold run**, and the cold run is the expensive part — which is why my
recommendation is conditional on ticket 15's rehearsal, whose defined sequence already includes "cold run
with Docling" (`.scratch/handwired-demo/issues/12-what-rehearsed-means-for-a-static-page.md`).

## 4. Can the page be truthful without it? Yes — and here is exactly what it must print

The page can be truthful because the *shape* of the run is already declared and the two durations are
already attributed at the only level they exist: the lane, per scan.

- **Attribution is legitimate at lane level.** The two Documents that route to OCR are declared
  (`run-summary.json` → `routing.ocr = 2`), each published Document's `engine`, `routing_decision` and
  `page_count` are declared (`publishing.py:45-53`; see `.demo/out/records/916943e79a51ecc5.json` in the
  pymupdf run, which is the same schema), and the runbook ties each figure to a scan by page count: "Docling
  took 54.5 s on the one-page scan and 73.0 s on the two-page scan"
  (`docs/demo/pdf-demo-runbook.md:140-141`). One page → 54.5 s, two pages → 73.0 s is therefore recoverable
  from declared page counts plus a labelled prose observation; nothing has to be invented.
- **Everything else is genuinely unmeasured**, and the page says so with a word the repo already uses:
  `not recorded` (`run_log.py:22`, `explorer.py:129-137`, `runbook:206-207`).

### What the page must print, option by option

**Option A — no new instrumentation (recommended for this page).**

| On screen | Exact wording that keeps it honest |
|---|---|
| run line / clock | `133.8 s total (observed)` — and its source is `out/last-run.json`, one run. |
| the two OCR cards | `54.5 s` / `73.0 s`, each carrying, *on the card or in a legend tied to the numbers*: `single warm-cache observation, not a benchmark`. The runbook's own limitation sentence is the template: "the 54.5 s / 73.0 s per-scan figures are single warm-cache observations" (`runbook:658`). |
| native lane / other 16 cards | `not timed individually`. Optionally `the run's remaining ≈6.3 s is a subtraction (133.8 − 54.5 − 73.0), not a measurement`. **Not** `0.39 s each` as if measured; **not** a per-card second count at all. |
| warm-rerun beat, if kept | `0 engine calls` is *declared* (`summary.extraction.calls`), and `8.6 s (observed)` belongs to `last-run.json`. It must not be attributed to any card. |
| compression label | the factor, the real total, and the one caveat that makes the pacing honest: `the two scans are drawn to scale; the other cards are paced by the remainder` — because at true scale they are nearly invisible (see below). |
| what must never appear | a per-card duration for a native Document; a sum of card times presented as the run total; any implication that the 54.5/73.0 were measured in *this* replay. |

**The arithmetic the page inherits (measured here, and it does not close).** If the two scans are drawn to
a 30–45 s playthrough at one constant compression, the remainder is 1.4–2.1 s of screen time and each of
the 16 native cards gets 0.09–0.13 s:

| Playthrough | Factor | Two scans | Everything else | Per native card |
|---:|---:|---:|---:|---:|
| 30 s | 4.46× | 28.6 s | 1.41 s | 0.088 s |
| 35 s | 3.82× | 33.4 s | 1.65 s | 0.103 s |
| 40 s | 3.35× | 38.1 s | 1.88 s | 0.118 s |
| 45 s | 2.97× | 42.9 s | 2.12 s | 0.132 s |

Two design consequences, both honesty constraints on ticket 11: a constant-factor replay makes every
non-OCR card effectively invisible (so if the page wants them watchable it is using **two scales** and must
say so), and the remainder it prints is a **subtraction that contradicts another recorded observation**:

> 133.8 − (54.5 + 73.0) = **6.3 s** for 16 native extractions + cleaning + language tagging + judging +
> deduplication + target-state apply, but the same non-extraction pipeline over a warm cache measured
> **8.6 s** (`runbook:137`), and the demo command's cold run was 148.2 s (`runbook:148`).

So the 6.3 s remainder is 2.3 s *smaller* than a measurement of work it is supposed to contain. Either the
per-scan figures are not on the same basis as the run total (they are "single warm-cache observations",
which suggests the Docling model was already loaded, while the cold run's first call paid for it), or one
figure moved between revisions. The page must not resolve this; it must **print the remainder as a
subtraction** (`≈6 s, by subtraction`) and state in its limitations that the three figures do not
reconcile to better than ~2 s. Anything else invites a visitor to add the cards up and catch the page.

**Option B — add the sidecar.** The page may then print, per OCR card, `54.5 s (measured, this run)` and,
for non-OCR cards, either a measured per-call time or `untimed — reused from the memo`. It must still print
the total from `last-run.json`, still label the whole run "not reproducible", and — because a reused call
has no duration — still show `not recorded` on a warm replay. It buys per-scan precision the page does not
need and does not remove the arithmetic mismatch, because a cold re-run in ticket 15 would replace the
133.8/54.5/73.0 triple wholesale (and then the sidecar and `last-run.json` are consistent with each other
while the *old* runbook row becomes historical).

## 5. Recommendation, cost, and the risk each option carries

**Recommend Option A for the page as specified, with Option B parked behind ticket 15's cold re-run.**

Why: the animation's claim is *shape* — "two scans were 127.5 s of 133.8 s" — and that shape is already
provable from a declared counter (`routing.ocr = 2`), two declared page counts, and two prose observations
that the repo is willing to print as single observations. The page does not need per-Document precision; it
needs to not pretend it has it. Option B costs ~120 lines plus a cold Docling run, would not improve the
central claim, and would force ticket 09's export contract to carry a fourth, non-declared input whose
absence on any warm replay has to be explained anyway. If ticket 15 re-runs cold regardless, Option B's
expensive half is already paid and the sidecar becomes a cheap, honest upgrade — that is the trigger to
revisit, not this ticket.

| Option | Cost | Risk it carries |
|---|---|---|
| **A — lane-level attribution** | ~0 code; ~6 lines of page copy + 1 runbook sentence; a decision recorded in tickets 08/11 | The two scan numbers become the only durations on screen, so the clock rests on three prose digits from a non-reproducible observation; a visitor who assumes they were measured *for this replay* is misled unless the label sits on the number and the `not timed individually` label sits on the rest. Second risk, newly found: the 6.3 s remainder contradicts the 8.6 s warm run, so the page must print it as a subtraction or it will state a number the repo's own table refutes. |
| **B — per-call sidecar** | ~120 lines (accumulator + writer + 3 tests + README/runbook) **and** one cold Docling run (~134–148 s plus models) | It creates a second observed artifact that invites the belief timing is a product; the exporter (ticket 09) must pass it through; a warm replay has an *empty* file that looks like "fast" unless labelled; and per-call seconds do not reproduce, so the page gains precision without gaining any right to call it reproducible. Real leak risk: a timer placed in the engine adapters or the domain record instead of the memo body would put an ephemeral number one refactor away from `DocumentRecord`, breaking `CODING_STANDARDS.md`'s reproducibility rule and ADR-0006. |

## Measurements taken

All on this machine, against the committed tree and the installed `.venv`. No network, no pipeline run, no
model, nothing installed or modified outside this note.

1. **The demo run's observed log and declared summary** (`.demo/out/`): `last-run.json`
   `elapsed_seconds: 1.388`; `run-summary.json` `routing {native:16, ocr:2}`, `extraction {requested:18,
   calls:2, reused:16}`, `published {documents:15, page_records:310}`, `engine {pymupdf@1.28.2}`, no time
   key.
2. **State database**: `strings -n 6 .demo/state.db/mdb/data.mdb` — keys begin `extract/<digest>`, values
   carry `pages/number/text/engine/name/detail`; every `time`/`duration` hit is inside NOAA PDF text, not a
   schema field. No timestamp field in the memo store.
3. **CocoIndex surface**: `inspect.signature` + `getdoc` on `ComponentStats`, `UpdateStats`,
   `UpdateHandle.stats`, `stats_group`, `StatsGroupHandle` → counters only, `by_component` keyed by
   processor name. `grep -rn "duration|elapsed|perf_counter|start_time|end_time"` over the installed
   package → only `timeout`/`deadline` machinery. `core.pyi:294,301,307` →
   `tuple[int, bool, dict[str, dict[str, int]]]`.
4. **Toy-app probe** (temp dir, own DB, one memoised fn × 3 items, one `stats_group`, no PDF, no network):
   `UpdateStats(by_component={'main': ComponentStats(num_execution_starts=1, num_unchanged=0, num_adds=1,
   num_deletes=0, num_reprocesses=0, num_errors=0)})`; fields exactly the six integers; `sg.stats()` is
   `None` inside the `with` body, as its docstring warns ("the group becomes ready asynchronously once the
   body has exited").
5. **Timeline arithmetic** (`python3`, from the runbook's figures): scans 127.5 s = 95.29% of 133.8 s;
   remainder 6.3 s; equal share over 16 native Documents 0.394 s; constant-factor playthroughs as tabulated
   in §4; cold remainder 6.3 s vs warm rerun 8.6 s ⇒ a 2.3 s (1.7%) mismatch between the remainder and an
   independent measurement of the same non-extraction work.
6. **Leak test precedent**: `tests/infrastructure/test_run_summary.py:183-200` already asserts
   `run-summary.json` bytes are identical across runs *and* `last-run.json` does not exist on the flow-only
   path; `:203-221` asserts the summary carries no observation of when the run happened;
   `tests/interfaces/test_cli.py:169-215` asserts the wrapper writes the observation beside the declared
   bytes. Option B extends these three, it does not invent a new discipline.

## Sources

- `src/finepdfs/infrastructure/cocoindex/flow.py:80-100` (memoised body, the only place an engine call is
  observable), `:145-151` (mount keyed by digest), `:211-223` (summary declared, no timing).
- `src/finepdfs/application/metrics.py:11-13` (CocoIndex stats deliberately excluded), `:26` (memo key),
  `:95-135` (`summary()`, counters only, no timing).
- `src/finepdfs/interfaces/run_log.py:1-9` (the declared/observed split), `:21-25` (file name, note),
  `:28-43` (`ObservedRun`, three fields), `:66-71` (writer, command-only).
- `src/finepdfs/interfaces/summary.py:17-29` (`conclude_run`, the one writer seam), `:32-53` (terminal line).
- `src/finepdfs/interfaces/cli.py:612-644`, `src/finepdfs/interfaces/demo.py:176-205` (both wrappers).
- `src/finepdfs/domain/publishing.py:45-53` (`DocumentRecord`, no duration), `:56-68` (page records),
  `:68-69` (exclusions).
- `src/finepdfs/application/curate.py:96-101` and `src/finepdfs/domain/ports.py:43`,
  `src/finepdfs/infrastructure/engines/{docling,pymupdf}_engine.py` (synchronous `extract`).
- `src/finepdfs/infrastructure/cocoindex/runtime.py:41-57` (`Runtime.metrics`, `compare=False`).
- `README.md:295-313` (output layout, declared-vs-observed), `docs/demo/pdf-demo-runbook.md:124-143`
  (measured runs, 133.8/54.5/73.0/8.6), `:179-209` (schemas, "not recorded"), `:641-665` (limitations,
  "single warm-cache observations"), `CODING_STANDARDS.md:40-50` (reproducible records), `docs/adr/0006`.
- Installed CocoIndex 1.0.25: `.venv/lib/python3.12/site-packages/cocoindex/_internal/update_stats.py:54-103`,
  `_internal/component_ctx.py:453-490`, `_internal/core.pyi:294-307`, `cocoindex-1.0.25.dist-info/METADATA`.
- Tickets: `.scratch/handwired-demo/map.md` ("A known gap"), `:11-the-animations-timeline-model.md`,
  `:09-the-exported-bundles-contract.md`, `:12-what-rehearsed-means-for-a-static-page.md`,
  `:08-every-claim-and-what-proves-it.md`.
