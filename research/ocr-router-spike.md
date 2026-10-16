# The FinePDFs OCR router: 124 features, not 609 — and what a port actually costs

*Question answered: what exactly is the router's feature vector, what does each feature need from the
PDF, how much of it do we already compute, what may we legally reuse, and should we build it? Verified
by reading the upstream clone at `/tmp/upstream-finepdfs` (read-only), parsing the shipped model
binaries, querying the Hugging Face APIs, and running an end-to-end extraction probe on this machine
with the PyMuPDF that is already installed.*

## Headline: the ticket's central number is wrong, and that changes the size of the job

The ticket — and `research/finepdfs-flow-implementation-map.md` — say the model wants a
**609-dimensional** vector. It does not. There are two model files in
`models/xgb_ocr_classifier/`, and they are different models:

| File | Size | `num_feature` | Referenced by any shipped code? |
|---|---|---|---|
| `xgb_classifier.ubj` | 257,625 B (252 KB) | **124** | **Yes** — the only path `run_finepdfs_pipeline.py` loads |
| `xgb_metadata_model.json` | 664,385 B | 609 | **No** — `grep` over every `.py`/`.sh`/`.toml`/`.md` finds no reference |

Both are Git LFS pointers in the clone, so both had to be fetched from
`media.githubusercontent.com`. The 609 artefact never reaches a prediction in the shipped pipeline, and
its own `feature_names` array is **empty**, so the 609 features cannot be named from it. Its purpose is
undetermined (see *Could not determine*, below). The router we would actually be chasing is the
124-wide one.

Even better: `xgb_classifier.ubj` **embeds its own ordered feature-name list**, which I parsed out of
the binary. It is exactly the column order that `flatten_per_page_features` produces, so the shipped
code and the shipped weights are fully consistent with each other — the mismatch was only ever between
the ticket and reality. That makes exact parity a feature-extraction job with a known target, not a
reverse-engineering job.

## 1. What the feature vector actually is

**124 = 4 document-level columns + 15 page-level families × 8 sampled pages (120).**

The training notebook printed `Number of features: 124` on its own run, which is the arithmetic
`4 + 15 × 8` with the `class` label column dropped before fitting. The 8-page count comes from
`num_pages_to_sample=8` / `PAGES_TO_EXTRACT=8`.

### The four document-level columns (in model order)

| # | Name | What it means |
|---|---|---|
| 1 | `num_pages_successfully_sampled` | How many of the sampled pages opened without raising. At inference the sample is capped at 8, so for any readable document of ≥8 pages this is the constant 8; it therefore only carries a *short-document* signal. It is nonetheless the single most-split-on column (7.4% of internal nodes) — a hint that document length correlates strongly with the OCR label. |
| 2 | `garbled_text_ratio` | U+FFFD replacement characters as a share of characters, **over the sampled pages only**, from `page.get_text("text", …)`. A doc-wide variant is computed too but is not in the emitted set. |
| 3 | `is_form` | `doc.is_form_pdf`, with `None` coerced to `False`. |
| 4 | `creator_or_producer_is_known_scanner` | Substring match of ~18 scanner-vendor keywords ("epson", "fujitsu", "kofax", "readiris", …) against the lowercased `creator`/`producer` metadata. |

Three further names appear in the *code's* document-level list but never in the vector: `class` is the
label; `num_unique_image_xrefs` and `num_junk_image_xrefs` have their assignment lines commented out, so
they are absent. Anyone reading only the list gets the arithmetic wrong — this is the exact trap the
ticket and the older note fell into.

### The fifteen page-level families

Each is computed per sampled page, then flattened into eight columns `_page1…_page8`.

| Family | What it measures |
|---|---|
| `unique_font_counts` | Distinct font names on the page. |
| `char_counts` | Characters in *visible* text spans. |
| `text_box_counts` | Number of visible text spans. |
| `avg_text_box_lengths` | Visible text bbox area ÷ visible span count (misnamed: an area, in points²). |
| `text_area_ratios` | Visible text bbox area ÷ page area. |
| `hidden_char_counts` | Characters in spans that are invisible (render mode 3) or fully transparent. |
| `hidden_text_box_counts` | Number of such spans. |
| `hidden_avg_text_box_lengths` | Hidden bbox area ÷ hidden span count. |
| `hidden_text_area_ratios` | Hidden bbox area ÷ page area. |
| `image_counts` | Image *instances*: for each image xref on the page, the number of placed rectangles. |
| `non_junk_image_counts` | The same, excluding xrefs classified as junk (see below). |
| `bitmap_proportions` | Total area of non-junk image rectangles, **after a strip-merge pass**, ÷ page area. |
| `max_merged_strip_areas` | Largest single merged strip area ÷ page area. |
| `drawing_strokes_count` | Vector stroke operations from the page's drawing paths. |
| `vector_graphics_obj_count` | Number of drawing paths on the page. |

"Junk" images are xrefs that repeat across pages — decorations, not content. The intent is a
majority-of-pages rule, but the threshold is computed and then **immediately overwritten** by the
sampled-page count, so the effective rule is: an xref is junk only if it appears on *every* sampled
page, and only when at least 3 pages were sampled. This is a bug in upstream that parity would have to
reproduce deliberately.

The strip-merge pass is the most bespoke piece of the whole vector: it de-duplicates image rectangles by
bbox, sorts by position, and merges a rectangle into its predecessor when it spans ≥90% of the page
width or height and its edges line up within a few points. It is tuned by four module constants and it
is what turns "this page has images" into "this page is a scan".

### How pages are sampled

One chunk of up to 8 page indices is drawn **without replacement from the whole document and then sorted
ascending**; the first chunk is the only one scored at inference. A document shorter than 8 pages is
padded to 8 columns by re-drawing existing page indices **with replacement**, so its trailing columns
repeat earlier pages.

The sampling uses an unseeded `random.sample`, so **the feature vector — and therefore the score — is
stochastic run to run** for any document with more than 8 pages. That is a real problem for a memoised
pipeline, and it is not something the notebook or the predictor addresses.

Training differs from inference in two ways worth knowing: every chunk of every document becomes its own
training row, and OCR-labelled documents longer than 32 pages are re-sampled `pages // 32` extra times to
counter the class imbalance. The result was 2,314 training rows and an ROC AUC of **0.9636**.

## 2. What each feature needs from the PDF

All of it is PyMuPDF, and all of the required API surface is already present in the version installed
here (**1.28.2**; upstream pins 1.26.1). I verified this by running a probe, not by reading docs.

| Feature family | Operation required | Availability |
|---|---|---|
| `num_pages_successfully_sampled` | `len(doc)`, `doc.load_page`, `page.rect` | free |
| `garbled_text_ratio` | `page.get_text("text", flags=TEXT_PRESERVE_WHITESPACE\|TEXT_MEDIABOX_CLIP)` + count U+FFFD | free, one call per sampled page |
| `is_form` | `doc.is_form_pdf` | free |
| scanner keywords | `doc.metadata` | free |
| `unique_font_counts` | `page.get_fonts(full=True)`, field 3 (font name) | one call per page |
| `char_counts`, `text_box_counts`, `avg_text_box_lengths`, `text_area_ratios` | `page.get_texttrace()`, filtering out hidden spans; needs `chars`, `bbox`, `type`, `opacity` | one call per page |
| `hidden_*` (four families) | the same `get_texttrace()` call, the complementary branch | free once texttrace is called |
| `image_counts`, `non_junk_image_counts` | `page.get_images(full=False)` then `page.get_image_rects(xref, transform=False)` | one call per image xref per page |
| `bitmap_proportions`, `max_merged_strip_areas` | the same rectangles, then the strip-merge heuristic | real work |
| `vector_graphics_obj_count` | `len(page.get_cdrawings())` | one call per page |
| `drawing_strokes_count` | `page.get_cdrawings()`, walking each path's `items` for line/curve/quad opcodes, plus stroked rect/quad paths | one call per page, non-trivial |

The probe built three synthetic PDFs (native text, an image-only "scan", and a text-only page) and
assembled the full vector: **124 columns, exact name order, every value present, and no missing
`get_texttrace` keys** on 1.28.2. A native page scored a text-area ratio of 0.097 and zero bitmap
proportion; the image-only page scored bitmap proportion 1.0, image count 1 and text ratio 0.0. The
signals separate the two cases cleanly, which is the whole point of the router.

Feasibility is therefore **not** in doubt: PyMuPDF-only, no new dependency, and the API is stable
between the pinned version and ours.

## 3. Overlap with what we already compute

`TextLayerQuality` computes five fields (three derived), and the pipeline is already doing a full
`get_text("text")` pass over every page. Measured against the 124 columns:

| Already available | Notes |
|---|---|
| `garbled_ratio` | Same U+FFFD idea, **different denominator**: ours counts Cc/Cf/Co/Cn + U+FFFD over the whole joined text of every page; upstream counts only U+FFFD over the sampled pages. Correlated, not equal. |
| `character_count` / `characters_per_page` | A different measurement of the same thing: ours is `len(get_text("text"))`; upstream sums `chars` over visible `texttrace` spans. Close enough to seed `page_level_char_counts`, not bit-identical. |
| `image_dominated_pages` | A weak cousin of the image families: ours counts pages with no text *and* at least one image; upstream counts image instances and their **areas**. Not the same signal. |
| `page_count` | Not `num_pages_successfully_sampled` (which is capped at 8), but `min(page_count, 8)` is derivable from it. |

| Cheap additions (one PyMuPDF call per page, no new dependency) | Covers |
|---|---|
| `doc.metadata` keyword match, `doc.is_form_pdf` | 2 doc-level columns |
| `page.get_fonts(full=True)` | `unique_font_counts` (8 columns, 12.7% of splits) |
| `page.get_texttrace()` | text box counts/areas, avg box area (32 columns, ~28% of splits), and the hidden-text families for free |
| `page.get_images` + `get_image_rects` | `image_counts` (8 columns) |
| `page.get_cdrawings()` | `vector_graphics_obj_count`, `drawing_strokes_count` (16 columns, ~11% of splits) |

| Real work | Why |
|---|---|
| strip-merge heuristic → `bitmap_proportions`, `max_merged_strip_areas` | Bespoke geometry with four magic constants, and the top feature family (13.1% of splits, and the top two page-level columns). Fidelity here *is* the router. |
| junk-image xref logic | Needs a two-pass structure (document pass, then page pass), reproduces an upstream bug, and only earns 0.7% of splits. |
| the 8-page resampling semantics | Trivial to write, but must be frozen to make scoring reproducible. |

So the honest count is: **three columns are effectively free, about seventy are cheap one-call
additions, and roughly twenty are genuine work.** Note what the importance numbers say about where that
work should go — the hidden-text families collectively earn ~2% of splits and **22 of the 124 columns
were never split on at all**, mostly hidden-text area ratios. The exotic, awkward-to-extract half is
also the half the model ignores. Only two families are both expensive and load-bearing:
`bitmap_proportions` (13.1%) and `max_merged_strip_areas` (5.3%), both downstream of the same strip-merge
pass.

Importance was computed by parsing the `.ubj` node arrays and counting splits on **internal nodes only**.
That filter matters: XGBoost writes `split_index = 0` on leaf nodes, so a naive count makes column 1 look
like it takes half the model. I confirmed the artefact by re-running the count on the unrelated 609-wide
JSON model, where the spurious index-0 dominance disappeared once leaves were excluded.

## 4. Licence: what may and may not be copied

Upstream is **AGPL-3.0**, and this repo carries **no `LICENSE` file** at all, so there is no declared
outbound licence to argue from — vendoring AGPL source into it is indefensible regardless of intent.

**May be copied / used:**

- **Factual names and order of the 124 features, and their numeric definitions.** These are facts
  recovered from a binary artefact and from the observable behaviour of the published model. Reporting
  them — as this note does — is not copying protected expression.
- **The annotated dataset `HuggingFaceFW/ocr-annotations`** to *train our own* model. Verified live:
  it exists, it is **not gated**, `license: odc-by`, one `train` split, **1,620 rows**, 1.41 GB of
  parquet (~1.5 GB in memory), 6 columns: `filename`, `pdf` (the PDF bytes are embedded), `annotation`
  ("scanned", "no-ocr-needed", …), `class` (`OCR`/`NOCR`), `truncation_type`
  (`truncated`/`non_truncated`), `pdf_size_bytes`. Class balance is roughly 227 OCR to 1,393 NOCR
  (14% OCR); upstream upsamples. ODC-By is an attribution licence: it permits commercial use and
  derivative works provided we attribute. Weights trained from it are ours; the attribution obligation
  travels with the data.
- **Our own extractor and our own model.** The intended clean path is to write the extraction ourselves
  from the semantics above and train a fresh classifier on the ODC-By set.

**May not be copied:**

- `blocks/predictor/ocr_predictor.py`, the training notebook, or any other FinePDFs source — all AGPL.
  No translation, no paraphrase-into-code, no copy-paste.
- `xgb_classifier.ubj` or any other upstream weights. Shipping their AGPL weights in this repo would
  make the whole repo AGPL-tainted.
- The `xgb_metadata_model.json` 609-wide model, for the same reason (and it is unidentifiable anyway).

**What a training run needs:** the 1.41 GB dataset, our own extractor, PyMuPDF (have it), and a fitting
library. The repo currently declares exactly two runtime dependencies (`cocoindex`, `pymupdf`); neither
`scikit-learn` nor `xgboost` is installed. Upstream's best grid-search config was `n_estimators=200,
max_depth=5, learning_rate=0.1, min_child_weight=3`. Cost is small: the notebook's 48-fit grid search
over 2,314 × 124 finished in seconds on CPU, and the production model is 200 trees. **The training run is
minutes of CPU, not a GPU job.** The real cost is the extractor, and one new dependency — which for a
two-dependency repo is a design decision, not an afterthought.

One more licence-adjacent point: their operational rule is `ocr_prob >= 0.2 **or** garbled_text_ratio > 0`,
i.e. a single U+FFFD in the sample forces OCR. Their threshold was tuned against their own estimated
GPU budget (their notebook models 24×8 GPUs, 5 pages/sec, 1.2B non-truncated documents). That threshold
is a compute-budget decision, not a property of the model, and would have to be re-tuned for us anyway.

## 5. Cost, risk and recommendation

**Effort to parity** (our own model, not their bytes):

| Piece | Estimate |
|---|---|
| Extractor: 4 doc-level + 15 page-level families, the strip-merge pass, the junk two-pass, the sampling semantics, plus tests | 3–4 days |
| Training harness: dataset download, balanced sampling, fit, export to a loadable artefact | 0.5–1 day |
| Port: an adapter behind the routing port, memo-key handling, and a routing-agreement measurement against the heuristic on the real corpus | 1–2 days |
| **Total** | **~1 week** |

That is roughly a third of what the 609 figure implied. Nothing here is research.

**The riskiest unknown is not the features.** Extraction is now known to be tractable and PyMuPDF-only.
The unknown is **whether the router actually routes better than our heuristic on the NOAA corpus we
have**. Their training set is 1,620 hand-annotated Common Crawl PDFs, 14% positive; the label is a human
judgement that a document "needs OCR"; and our corpus, our engine mix and our GPU cost are all different.
A 0.96 AUC on their test split is no evidence at all about our corpus. There is a real chance the honest
outcome is "we cannot tell without hand-labelling our own 300 documents", which is a worse job than the
extraction.

The second unknown is reproducibility: the vector depends on an **unseeded random 8-page sample**, so
the same PDF scores differently on successive runs. Inside a content-addressed, memoised pipeline that is
a design flaw to inherit. We would have to seed the sample and freeze the seed into the engine identity,
and then re-measure agreement.

**The cheapest experiment that de-risks it** — and the reason this is not a "go":

We already have the whole first half. `measure_text_layer()` already walks every page of a PDF with
PyMuPDF and computes characters per page, coverage, garbled ratio and image-dominated pages. So:

1. Download the 1.41 GB annotated set (it is ungated) and run **our existing probe** over all 1,620
   PDFs — no new extraction code, no new dependency.
2. Fit a small classifier (logistic regression or a shallow GBM) on our existing signals only, and
   compare cross-validated AUC against upstream's 0.9636 on the same 1,620 documents.
3. If a handful of our signals get within a few points of their 124-feature model, **we do not need
   `get_cdrawings`, the strip-merge pass, or the hidden-text families at all**, and the remaining work
   collapses to the cheap column set plus a threshold. If it lands far short, we have a number that
   justifies the full week.

This costs a fraction of a day, touches no production code, and turns "should we build the router?" into
a measurement. It is the experiment the ticket should have asked for first.

**Recommendation: go-later — build the signals before the model.**

The spike's real result is that the router is much closer than the ticket thought, but that the
*ordering* of the work was wrong. Concretely:

- **Now (cheap, no new dependency):** add the four cheap, high-value families to the probe —
  font diversity, text-box counts and areas, image instance counts and rectangle areas, and drawing
  counts. These are one PyMuPDF call per page, they are ~50% of the shipped model's split mass, and they
  improve `TextLayerQuality` and the heuristic *whether or not* a model ever sits on top. This is
  explainable, testable, and does not touch the memo key.
- **Then:** run the cheap experiment above.
- **Later, only on a positive result:** the model adapter, with a seeded sample, its identity in the
  memo key, and a routing-agreement report against the heuristic on the real corpus — exactly as the
  ticket's remaining acceptance criteria already say.

A no-go would be wrong: the port is a known, bounded, one-week job. An immediate go is also wrong: it
spends the week before anyone has checked that the model beats what we have.

## Could not determine

- **What `xgb_metadata_model.json` (609 features) is.** It sits beside the router, is referenced by
  nothing in the repo, has no feature names, and `num_feature=609`. It dominates nothing in the
  pipeline. Its name suggests a metadata-derived classifier, possibly an internal artefact committed by
  accident. I could not name its features or establish its role. Anyone acting on the ticket's "609"
  figure should treat it as unverified.
- **Whether the shipped `.ubj` was trained by the shipped notebook's exact code path.** The feature order
  matches perfectly, which is strong evidence, but the notebook's own printed run says 124 while the
  model stores 200 trees and the notebook's grid could select `n_estimators` in {100, 200}; the artefacts
  are consistent with the notebook's *best* configuration, not necessarily with the printed cell output
  I read.
- **Whether upstream's model reproduces on PyMuPDF 1.26.1 vs the 1.28.2 installed here.** `get_texttrace`,
  `get_cdrawings` and `is_form_pdf` all behave as the code assumes on 1.28.2 in my probe, but no
  bit-for-bit comparison against 1.26.1 was possible without installing `xgboost` (which I did not add,
  per the ticket's rules).
- **Our corpus's OCR label distribution.** I have not run anything against the NOAA corpus; the
  routing-agreement measurement the ticket asks for is still owed.
- **`garbled_text_ratio` could not be exercised** by my probe: PyMuPDF's writer re-encodes U+FFFD on
  `insert_text`, so a synthetic "garbled" PDF came back clean. Testing that one feature needs a real
  broken-encoding PDF from the corpus.
