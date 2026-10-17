# De-risking the learned router: the existing signals reach 0.804 AUC, 0.159 short — ticket 09 proceeds

*Ticket 13. "Run the probe we already have (`TextLayerQuality`) over the 1,620 PDFs in
`HuggingFaceFW/ocr-annotations`, fit a small classifier on those existing signals alone, and compare its
AUC against the 0.9636 FinePDFs reports for their 124-column model." Measured on this machine with the
PyMuPDF 1.28.2 already installed, against the ODC-By annotated set, with the repo's probe imported
unchanged. Nothing in this note is a production change: no probe feature was added, no dependency was
added, and no dataset was committed.*

## The answer, up front

**The eight signals `TextLayerQuality` already computes reach a held-out ROC AUC of 0.804, 0.159 below
the 0.9636 FinePDFs reports for its 124-column model.** A five-fold cross-validation puts the same model
at 0.833 ± 0.016, and the most favourable variant tried (log-scaled counts) reaches
0.851; none approaches 0.96, and the primary model's bootstrap 95% CI upper bound, 0.878, still sits 0.086
below their number. **The gap is wide enough, and the *shape* of the error pointed enough, that the
expensive half of ticket 09 is justified: proceed to build the remaining columns**, front-loading the
families the error shape names.

## What was fetched, and its licence

| | |
|---|---|
| Dataset | [`HuggingFaceFW/ocr-annotations`](https://huggingface.co/datasets/HuggingFaceFW/ocr-annotations) |
| Revision | `48a1a17645b0a24460f18b77c1184a6f0e6f4a2b` |
| Licence | **`odc-by`** (Open Data Commons Attribution License) — ungated, no token needed |
| Fetched | the four `data/*.parquet` shards only, 1.41 GB total (the embedded-PDF column is required to run the probe) |
| Lives at | `/home/ubuntu/tmp/router-derisk/ocr-annotations/` — **outside every worktree**, gitignored by omission, not committed |
| Rows | 1,620: 227 `OCR` (14.0%) and 1,393 `NOCR`; `annotation` splits into 1,393 `no-ocr-needed`, 159 `partial-ocr-needed`, 68 `scanned` |

ODC-By is an attribution licence: it permits commercial use and derivative works with attribution. The
attribution obligation travels with the data, so this note carries the dataset name, revision and licence
as the record of what was used. Nothing from the dataset — no PDF bytes, no labels, no derived table —
enters git.

## How it was measured

- **The probe is unchanged.** `extract_features.py` imports `PymupdfTextLayerProbe` from
  `src/finepdfs/infrastructure/engines/` by `PYTHONPATH` and calls `.measure(content)` on each embedded
  PDF. It computes no feature of its own; it records the five fields the probe returns plus the three
  properties the domain object already derives (`characters_per_page`, `text_coverage`,
  `image_dominated_share`). **No new extraction code exists anywhere in this measurement.**
- **All 1,620 PDFs probed, 0 failures.** Every document opened and measured; the probe's `PdfReadError`
  path was never taken. (MuPDF printed format warnings for a handful of pages; none became an error.)
- **The model is deliberately small and standard.** Logistic regression on the eight signals, wrapped in
  a `StandardScaler` (counts and ratios differ by orders of magnitude), `max_iter=5000`.
- **The split is seeded.** A stratified 70/30 hold-out via
  `train_test_split(test_size=0.30, random_state=13, stratify=y)`: 1,134 train rows (159 OCR) and 486 test
  rows (68 OCR). Every reported AUC is on the **held-out** 486; train AUC is shown only to prove the model
  is not overfitting (0.811 vs 0.804).
- **Uncertainty is reported, not implied.** A seeded (seed 13) 4,000-resample bootstrap of the held-out
  AUC gives the 95% interval below, because 68 positive test rows makes a point estimate alone too
  confident.

## Results

Held-out AUC, one seeded split, 486 test documents.

| Model | Train AUC | **Held-out AUC** | Gap to 0.9636 |
|---|---|---|---|
| **Logistic regression, 8 existing signals (primary)** | 0.811 | **0.804** | **0.159** |
| Logistic regression, 5 raw fields only | 0.795 | 0.766 | 0.197 |
| Logistic regression, class-weighted | 0.808 | 0.797 | 0.166 |
| Logistic regression, log-scaled counts | 0.835 | 0.851 | 0.113 |
| Shallow tree, depth 1 / 2 / 3 / 4 | — | 0.809 / 0.833 / 0.833 / 0.825 | ≥ 0.131 |

- **Primary model: held-out AUC 0.8042, 95% bootstrap CI [0.725, 0.878], bootstrap SE 0.038.** Its
  five-fold cross-validated AUC is **0.833 ± 0.016** (folds 0.816–0.853), so the hold-out point estimate is
  a little pessimistic but the interval is the honest reading.
- **Compare each signal in the direction that predicts OCR.** Negating the reported text-volume
  scores gives approximately: `-characters_per_page` **0.82**, `-character_count` **0.80**,
  `-text_coverage` **0.79**, `image_dominated_share` **0.781**,
  `image_dominated_pages` **0.780**, and `-pages_with_text` **0.76**. These complements
  use the rounded raw AUCs (0.18, 0.20, 0.21 and 0.24), so the reversed values are approximate.
  Text density is therefore stronger than image share among these reported single signals.
  Logistic regression can use decreasing text-volume signals directly through negative coefficients;
  an interaction with image share is not required. This ranking alone does not establish which
  family adds information conditional on the other signals.
- **`garbled_ratio` is nearly useless here** (single-feature AUC 0.44): 300 documents have any garbling
  and 282 of them are `NOCR`. Upstream's operational rule "`garbled_text_ratio > 0` forces OCR" would flag
  almost a fifth of the corpus, overwhelmingly negatives. That rule is a compute-budget lever tuned against
  their corpus, and it does not transfer.

## The gap, and why the comparison is approximate

The gap is **0.9636 − 0.8042 = 0.1594 AUC**. In rank terms, a random OCR document outranks a random NOCR
document 80.4% of the time with our eight signals and 96.4% of the time with their 124. The
hold-out's best case (CI upper bound 0.878) is still 0.086 short, so this is not a small-sample artefact.

**0.9636 is their model on their labels, and this number is ours on the same labels. Five differences make
the comparison approximate, and none of them can be removed from the artefact that reports 0.9636:**

1. **Training size and row granularity.** They trained on **2,314 page-chunk rows**, re-sampling every
   8-page chunk of each document and upsampling long OCR documents; we fit 1,134 document rows, one per
   PDF. A chunk-level model sees multiple views of the same document.
2. **Feature count.** 124 columns versus 8. This is the variable the measurement exists to price.
3. **Model class.** Theirs is XGBoost after a grid search; ours is a regularised linear model.
   The depth-1–4 single-tree experiments do not establish what XGBoost could achieve with these
   same eight signals. Without a matched model comparison, the share of the gap due to model
   capacity versus missing features is unmeasured; no feature ceiling follows from these results.
4. **Split and sampling seed.** Our split is seeded and named (`random_state=13`). Their training sampled
   pages with an **unseeded** `random.sample`, and the 0.9636 comes from their notebook's own partition,
   which cannot be reproduced from the shipped artefact. The two test sets are not comparable row for row.
5. **Label unit.** Ours is the document's `class`; their row label comes from page-chunk annotations of the
   same set, so the positive *prevalence* and the difficulty mix differ even though the source is the same
   1,620 documents.

What *is* the same is the dataset and the human judgement behind its labels: both numbers are measured
against ODC-By annotations of the same 1,620 Common Crawl PDFs, so the comparison is meaningful even
though it is approximate.

## The shape of the error: the probe sees that text exists, not whether it is right

The confusion below uses a threshold chosen **on the training split only** (Youden J, 0.0665), so the test
confusion is not tuned on test. On the 486 held-out documents: **382 true negatives, 36 false positives,
21 false negatives, 47 true positives** — sensitivity 0.691, specificity 0.914, precision 0.566.

**Every false negative is a document with a full text layer.** All 21 missed OCR documents have
`text_coverage == 1.0` and `image_dominated_share == 0.0`: the probe found text on every page and no
image-dominated page, and concluded "native text" while the annotator said OCR was needed. The split by
label bucket is stark:

| Held-out OCR documents | n | Missed by the probe's model | Median text coverage |
|---|---|---|---|
| `text_coverage == 0` (no text anywhere) | 38 | **0** | 0.0 |
| `text_coverage > 0` (some text) | 30 | **21** | 1.0 |

So the existing signals are perfect on the pure-scan tail and near-blind on the rest: 30 of 68 test OCR
documents (44%) have *some* text layer, and the model misses 21 of those 30. The misses cluster where the
human judgement is about page *quality* rather than *presence*:

- **14 of 44 `partial-ocr-needed` documents missed (31.8%)** — the annotation whose whole meaning is "there
  is text, but not the text you need".
- **7 of 24 `scanned` documents missed (29.2%)** — scans whose text layer is an artifact the probe cannot
  distinguish from real text.

The false positives are the mirror image: **36 NOCR documents called OCR, 19 of them of ≤2 pages and 10
with `image_dominated_share > 0.5`** — short, image-only documents (covers, single-figure files) that a
human judged not worth OCR. `image_dominated_pages` fires on any page with no text and an image, so it
cannot tell a genuine scan from a one-page illustration.

This is exactly the ground the expensive columns are meant to cover. The probe measures *presence*:
does any character exist on a page, and is there an image where there is no text. The 124-column vector
measures *geometry and visibility*: visible-versus-hidden text via `get_texttrace` (spans, areas, render
mode 3), image instance counts and bitmap **areas** through the strip-merge pass, and font diversity. A
document with a low-quality or partially hidden text layer is indistinguishable from a clean one under the
eight signals, and that is the dominant failure mode measured here.

## The decision: proceed to build the remaining columns (ticket 09 does not close)

The measurement's kill condition was "our existing signals already approach theirs". 0.804 against 0.9636
is not approaching: the gap is 0.159 AUC, the CI never reaches their number, and the error shape localises
the shortfall to page-level quality that the existing signals structurally cannot see. **Abandoning ticket
09 would be the wrong read of this experiment.** The error analysis motivates testing the remaining columns, but does not prove they will close
this gap or that a stronger model on the existing signals cannot close it.

Priority follows the error, not the raw column count:

1. **`get_texttrace` visible/hidden families and text-box areas** — directly addresses the 21 false
   negatives that have text but still need OCR, by distinguishing real visible spans from hidden ones.
2. **Image/bitmap area and the strip-merge pass** (`bitmap_proportions`, `max_merged_strip_areas`) — the
   top feature family in their model and the only way to tell a scan from a figure, which is the false-
   positive shape here.
3. **Font diversity and drawing counts** — cheap one-call additions that carry ~25% of their model's split
   mass.

Two things this measurement did *not* establish, and the build should not assume them: it does not price
the gap *between* feature count, row granularity and model class (a GBM on these eight signals might close
some or all of it), and it says nothing about routing quality on *our* NOAA corpus, which ticket 09 still owes as
a separate measurement.

## Reproducing it

The runnable scripts are now included in `research/router_derisk/`. They reconstruct the
documented method; the original throwaway files were not available when they were added.
**The historical figures above have not been revalidated with these replacements.** In particular,
the original note did not preserve feature-column order, shard/row order, CV shuffle settings, tree
settings beyond depth, or the exact dependency versions other than PyMuPDF. The scripts make those
choices explicit: sorted shard filenames with original row order, the feature order in `model.py`,
shuffled stratified five-fold CV with seed 13, default trees with seed 13, and log1p on page count,
pages with text, character count, image-dominated pages, and characters per page. Treat differences
from the historical figures as a reproduction discrepancy to investigate, not as confirmed results.
The JSON outputs record versions, input hashes, split filenames, per-signal AUCs, bootstrap interval,
training-selected threshold, confusion counts and error rows.

Run these Bash commands **from the checkout root**, with Python 3.11 or newer. Dataset, venv,
features and results all go to an external temporary directory; none should be committed.

```bash
# 1. Isolated research environment; project dependencies are untouched.
RUN_DIR="$(mktemp -d)"
python -m venv "$RUN_DIR/venv"
PY="$RUN_DIR/venv/bin/python"
"$PY" -m pip install pymupdf==1.28.2 pyarrow huggingface_hub scikit-learn numpy pandas

# 2. Fetch the recorded revision (ungated, ODC-By), including embedded PDF bytes.
"$RUN_DIR/venv/bin/hf" download HuggingFaceFW/ocr-annotations \
    --repo-type dataset --revision 48a1a17645b0a24460f18b77c1184a6f0e6f4a2b \
    --include "data/*.parquet" --local-dir "$RUN_DIR/ocr-annotations"

# 3. Call the checkout's unchanged probe, one row per PDF.
PYTHONPATH=src "$PY" research/router_derisk/extract_features.py \
    --dataset "$RUN_DIR/ocr-annotations" --output "$RUN_DIR/features.csv"

# 4. Seeded split, model variants, bootstrap CI, and error counts.
"$PY" research/router_derisk/model.py \
    --features "$RUN_DIR/features.csv" --output "$RUN_DIR/model_results.json"
```

The primary procedure uses `train_test_split(test_size=0.30, random_state=13, stratify=y)`,
`StandardScaler` then `LogisticRegression(max_iter=5000, random_state=13)`, and a
`numpy.random.default_rng(13)` bootstrap of 4,000 held-out resamples (single-class resamples
are skipped and the valid count is recorded). The threshold maximises training Youden J;
ties use the first threshold returned by `roc_curve`. Held-out labels never select a threshold.
