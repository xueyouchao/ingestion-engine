# Would a per-language publish threshold change any decision? — historical measurement

*Ticket 14, first checkbox: "on the committed corpus, how many documents does the global threshold
exclude whose own language's FinePDFs threshold would keep, and vice versa? If that set is empty, park
the ticket and let the measurement be the deliverable." Measured against the corpus on this machine
(12 files, `CC-MAIN-2026-39`, `noaa.gov`, 292 pages) with the shipped `lid.176` adapter and FinePDFs'
own `thresholds/th_values.json`, not inferred from either table alone.*

## Input provenance — unresolved

**The original inputs are not pinned.** The phrase “committed corpus” in the ticket quoted above
was inaccurate: neither the PDFs nor their measured `corpus/manifest.json` is tracked. The crawl ID,
domain, filenames and page counts below do not identify the PDF bytes. No immutable copy of that
manifest has been located, so the reported results are historical observations, not a reproducible
baseline. A fresh query of the same crawl must not be presented as recovery of the measured inputs.

To resolve this gap, recover the manifest from the original measurement run and preserve it as
`research/per-language-thresholds-corpus-manifest.json` (or link an immutable copy). It must include
all 12 entries with `url`, PDF SHA-256 `digest`, `file`, `bytes`, `crawl_id`, `warc_filename`,
`offset` and `length`, matching the existing `CorpusManifest` schema. Recover each PDF from its
Common Crawl WARC byte range, then verify the extracted payload's SHA-256 and byte count before
running the probe. This follows [ADR-0002](../docs/adr/0002-real-common-crawl-corpus.md); PDFs need
not be committed, but the measured manifest must be preserved.

If the original manifest cannot be recovered, run a new measurement and commit its manifest together
with the replacement results. Do not assign newly fetched digests or locations to the old numbers.
Until either happens, the zero-change result below cannot independently justify parking ticket 14.

## The reported answer (unverified)

**0 documents change decision in either direction.** The global floor of 0.65 excluded 0 of the 12
documents, so there is no exclusion for a per-language threshold to reverse; and both languages the
corpus contains have FinePDFs thresholds *below* 0.65 (`eng_Latn` 0.395, `spa_Latn` 0.243), so the
per-language rule is strictly more permissive here and also excludes nothing the global rule kept.

| Direction | Documents |
|---|---|
| Global drops, per-language would keep | **0** |
| Global keeps, per-language would drop | **0** |
| Total documents whose verdict changes | **0** |

**The original run parked ticket 14; that conclusion awaits input provenance or a new pinned run.**
Nothing is built: no ADR, no threshold table in
the repo, no change to `PublishPolicy` or `LanguagePolicy`. The caveat that matters is in "N = 12, two
languages" below: this measures *this* corpus, and the table's own distribution says a corpus with
different languages could well flip the other way.

## What "the decision" means here

Both policies run over the same per-page tags from the shipped adapter, so the only difference is the
threshold:

- **Global.** `LanguagePolicy(min_confidence=0.65)` keeps a page whose confidence clears 0.65, picks the
  document language by majority of the kept pages, and `PublishPolicy.judge` publishes or drops. This is
  the shipped code, unchanged.
- **Per-language (P1).** The same page loop, with the floor taken from the FinePDFs table keyed on the
  language *that page* was named, falling back to 0.65 when the language has no entry. This is the direct
  structural analogue of `LanguagePolicy`, which is the object that owns `min_confidence`.
- **Per-language (P2).** FinePDFs' own shape: name the document language and its aggregate confidence
  with no floor, then keep the document if that confidence clears its language's table entry. Reported as
  a cross-check on P1, not as the primary reading.

FinePDFs keys its table by `language_script` (`eng_Latn`); `lid.176` names no script. The key is resolved
by unique prefix match (`eng_` → `eng_Latn`), and both languages here resolve to exactly one key.

## The corpus

12 PDFs, `CC-MAIN-2026-39`, `noaa.gov`, extracted with the default `pymupdf` native text layer and the
`for_native_text()` cleaning rules. 292 pages, 481,407 characters.

| | |
|---|---|
| Documents | 12 |
| Published by the global policy | 12 |
| Dropped by the global policy | 0 |
| Document languages (`lid.176`) | 11 × `eng`, 1 × `spa` |
| Lowest document confidence (no floor, P2's input) | 0.8531 |
| Lowest document confidence (global 0.65 floor) | 0.8751 |
| Highest document confidence | 0.9483 |

The Spanish document (`NOAA_en_el_Caribe_Edicion_Verano_2022_Espanol.pdf`) is the one the script adapter
cannot separate; ticket 08 already established that. It is not near the floor: 0.948.

## The measurement

Every document published (`P`), the per-language rule's key and threshold, and the accepted-page counts:

| document | pages | global | conf | key | T | global | P1 | P2 | pages global | pages P1 |
|---|---|---|---|---|---|---|---|---|---|---|
| `NOAA-in-the-Caribbean-2021-Summer-Newsletter-English.pdf` | 11 | eng | 0.891 | `eng_Latn` | 0.395 | P | P | P | 11 | 11 |
| `Additional-Information-NOAA-tribal-provisions-in-IIJA.pdf` | 4 | eng | 0.884 | `eng_Latn` | 0.395 | P | P | P | 4 | 4 |
| `NOAA-in-the-Caribbean-Fall-2021-Newsletter-English.pdf` | 11 | eng | 0.926 | `eng_Latn` | 0.395 | P | P | P | 11 | 11 |
| `AwardActionRequestGuidance_MAR2022B.pdf` | 41 | eng | 0.904 | `eng_Latn` | 0.395 | P | P | P | 41 | 41 |
| `NOAA_en_el_Caribe_Edicion_Verano_2022_Espanol.pdf` | 16 | spa | 0.948 | `spa_Latn` | 0.243 | P | P | P | 16 | 16 |
| `NOAA_in_the_Caribbean_Summer_2022_Newsletter_English.pdf` | 15 | eng | 0.905 | `eng_Latn` | 0.395 | P | P | P | 15 | 15 |
| `CELC_2022_Educators_Workshop-Summary_Report.pdf` | 25 | eng | 0.875 | `eng_Latn` | 0.395 | P | P | P | 24 | 24 |
| `Facilitators-Guide-to-MWEE-Training-2023.pdf` | 84 | eng | 0.914 | `eng_Latn` | 0.395 | P | P | P | 80 | 84 |
| `NOAA-in-the-Caribbean-Autumn-2022-Newsletter_English_Final.pdf` | 18 | eng | 0.884 | `eng_Latn` | 0.395 | P | P | P | 18 | 18 |
| `IIJATribalProvisionsNOAAExecutiveSummaryandResponse.pdf` | 12 | eng | 0.907 | `eng_Latn` | 0.395 | P | P | P | 12 | 12 |
| `IRATribalNOAAExecutiveSummaryandResponse_0.pdf` | 10 | eng | 0.922 | `eng_Latn` | 0.395 | P | P | P | 10 | 10 |
| `NOAA_Tribal_Consultation_Handbook%202023_FINAL.pdf` | 45 | eng | 0.917 | `eng_Latn` | 0.395 | P | P | P | 44 | 44 |

**Document verdicts:** 0 changes in either direction. P1 and P2 disagree on 0 documents.

**Page verdicts change on 4 pages of one document.** The global floor drops 4 pages of the 84-page
`Facilitators-Guide-to-MWEE-Training-2023.pdf` that the `eng_Latn` threshold of 0.395 accepts:

| page | language | confidence | global 0.65 | `eng_Latn` 0.395 |
|---|---|---|---|---|
| 1 | eng | 0.4607 | dropped | accepted |
| 59 | eng | 0.5165 | dropped | accepted |
| 64 | eng | 0.4513 | dropped | accepted |
| 84 | eng | 0.4462 | dropped | accepted |

That moves the record's `accepted_pages` from 80 to 84 and changes nothing about whether the document is
published. Two more pages sit below both thresholds: `CELC…Report.pdf` page 2 (`eng`, 0.3249, below even
the per-language 0.395) and `NOAA_Tribal_Consultation_Handbook…FINAL.pdf` page 1, which `lid.176` cannot
name at all. The unnamed page has no table entry, so P1 fell back to the global 0.65 and dropped it —
the fallback behaved as the ticket requires, though it never decided a document here.

## Why the answer is 0, and why that is not the whole story

The number is 0 for a structural reason, not a lucky one:

1. The global floor excluded no document, so the "global drops / per-language keeps" set is empty by
   construction.
2. The two languages present both have thresholds below the global floor, so the per-language rule is a
   strict relaxation of the global one on this corpus. The "global keeps / per-language drops" direction
   needs a language with a threshold *above* 0.65; neither `eng_Latn` nor `spa_Latn` is one.

The table says that second condition is common in general: of 1,545 entries, **1,106 are above 0.65 and
750 are above 0.9** (439 at or below 0.65; range 0.100 to 1.000). On a corpus carrying those languages
the per-language rule would very often be *stricter*, dropping documents the global floor keeps. The
measured local corpus simply does not contain one.

## N = 12, two languages: limits of the reported result

- 12 documents, 292 pages, 2 languages (`eng`, `spa`), one domain (`noaa.gov`), one crawl. It cannot say
  what happens on a language whose threshold is above 0.65.
- The direction the corpus cannot exercise is the live one: most of the table is above the global floor,
  and the feature's stated motivation is the opposite direction (languages whose confidence is
  systematically lower). Both directions deserve a corpus that contains the languages in question.
- A real answer needs a corpus spanning languages with FinePDFs thresholds on both sides of 0.65 —
  ideally with documents whose confidence falls between 0.65 and their language's entry, so the two
  policies actually disagree. That is a corpus change, not a machinery change.

**The exact measurement that would change the answer:** run the same probe over a corpus containing a
document in a language with a FinePDFs threshold above 0.65 (1,106 of 1,545 qualify) whose aggregate
confidence lands between 0.65 and that threshold — the global rule keeps it and the per-language rule
drops it — or a document the global rule drops entirely (every page below 0.65) whose language's entry
is low enough and whose pages clear it. Finding at least one such document converts the question from a
12-document corpus into a rate and unblocks ticket 14.

## Port FinePDFs' 1,545 values verbatim, or fit our own? — evidence, not a decision

Nothing is built, so there is no ADR and no decision of record. The evidence gathered while measuring,
which any future build must start from:

- **The table was fitted to a different classifier.** FinePDFs' README states it uses **GlotLID**, applied
  per page and averaged. `thresholds/find_th.py` fits each value on
  `HuggingFaceFW/finepdfs_lang_classification` using the field `best_page_average_score`, optimising a
  precision-weighted F-score. Our adapter is **`lid.176`**, with its own calibration, its own
  paragraph-chunk averaging (1,200 characters) and no script in the label. Porting 1,545 numbers fitted
  to GlotLID would port GlotLID's calibration onto lid.176 scores — the same class of risk ticket 13
  exists to de-risk for the OCR router.
- **The provenance is not licence-clean to vendor.** The values live in the `huggingface/finepdfs` code
  repository, which is **AGPL-3.0**; the underlying classification dataset is **ODC-By 1.0**. The model
  we already ship is CC-BY-SA-3.0 and downloaded, not vendored. A future decision about shipping a table
  in-repo has to reconcile those, not just the numbers.
- The comparison above applies GlotLID-fitted thresholds to lid.176 confidences. That is deliberate — the
  ticket asks for exactly this comparison — and it does not affect the parking conclusion, which rests on
  the global rule excluding 0 documents and on two thresholds being below 0.65. It does mean the numbers
  would not be a calibrated equality even if the set were non-empty.

## What was deliberately not built, and what a future build must add

Parked means parked: there is no `LanguagePolicy` per-language table, no second policy object, no
threshold data file, and no ADR, because there is no decision to record. The ticket's remaining
checkboxes were read and are recorded here so a future build starts from them:

- **Where the policy lives.** Still open. `PublishPolicy` owns the global floor today; a table-owning
  policy beside it is the alternative the ticket names.
- **Threshold visibility in the record.** Today a language-floor drop reports the generic reason
  "no page carried a language confident enough to publish" (`PublishPolicy.judge`), naming neither the
  confidence nor a threshold. A per-language rule would have to name *which* threshold decided, or a
  reviewer still could not audit an exclusion. That gap exists now and is out of scope while parked.
- **Fallback that says so.** P1's fallback to 0.65 fired once in this measurement (the unnamed page) and
  was recorded; the shipped code has no such thing to say yet.

## Reproducing the measurement

The original run cannot currently be reproduced: its corpus manifest is missing (see input provenance
above). The procedure below is a probe for a supplied local corpus, not a way to recover the original
12 inputs. For a new run, preserve the exact manifest alongside its results and verify every PDF
against its manifest digest and byte count before measuring.

Read-only over the corpus; no writes to the checkout. Two artifacts are fetched
**outside the repo** and never vendored:

| artifact | source | licence | sha256 |
|---|---|---|---|
| `lid.176.ftz` | `https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz` | CC-BY-SA-3.0 (per `FastTextModel`'s docstring) | `8f3472cfe8738a7b6099e8e999c3cbfae0dcd15696aac7d7738a8039db603e83` |
| `th_values.json` | `https://raw.githubusercontent.com/huggingface/finepdfs/main/thresholds/th_values.json` | repo AGPL-3.0; dataset ODC-By 1.0 | `2935c7adec19806b052fef4baa44bc366eaaba6b7aa3ca3cd3d98af38778c526` |

The optional `fasttext` extra was installed into a throwaway `--target` directory and put on
`PYTHONPATH`; `pyproject.toml` and the project's dependencies are unchanged. The script below is
`/tmp/measure_per_language_thresholds.py` (outside the repo); it prints the corpus inventory, the global
verdicts, the per-language comparison, the page-level changes, and the two counts.

Save the Python block below as `/tmp/measure_per_language_thresholds.py`, then run these commands
from the checkout root (with the project dependencies installed in `.venv`). By default, the script
imports from `./src` and reads `./corpus`. To use a different checkout or corpus, set
`INGESTION_ENGINE_ROOT` or `INGESTION_ENGINE_CORPUS` to its path; relative paths are resolved from the
current working directory.

```sh
python -m pip install --target /tmp/lidlibs "fasttext-numpy2-wheel>=0.9.2"
curl -o /tmp/lid176.ftz https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz
curl -o /tmp/fp_th_values.json https://raw.githubusercontent.com/huggingface/finepdfs/main/thresholds/th_values.json
PYTHONPATH=/tmp/lidlibs .venv/bin/python /tmp/measure_per_language_thresholds.py
```

Historical tail (only expected for the original, currently unpinned inputs):
`global decisions: published 12 dropped 0`, `global-drop / per-language-keep: 0`,
`global-keep / per-language-drop: 0`, `P2 disagreements with P1: 0`, `page-level trust changes … 4`,
`table values <= global floor: 439 > global floor: 1106 > 0.9: 750`.

### The measurement script

```python
"""Ticket 14, first checklist item: does a per-language publish threshold change any decision?

Read-only over a local corpus with a preserved manifest. Measures two decision functions over the
same per-page lid.176 tags and counts how many documents they decide differently:

  GLOBAL   LanguagePolicy(min_confidence=0.65) -> PublishPolicy.judge
  PER-LANG a per-page floor keyed on the page's predicted language, falling back to the global floor
           for a language with no FinePDFs entry (P1); and the document-level variant, the document's
           aggregate confidence against its language's entry (P2).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

CHECKOUT = Path(os.environ.get("INGESTION_ENGINE_ROOT", ".")).expanduser().resolve()
sys.path.insert(0, str(CHECKOUT / "src"))

from finepdfs.domain import text as text_rules
from finepdfs.domain.document import Document
from finepdfs.domain.language import LanguagePolicy
from finepdfs.domain.publishing import PublishPolicy
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextEngine
from finepdfs.infrastructure.language.fasttext import FastTextLanguageIdentifier, FastTextModel

CORPUS = Path(os.environ.get("INGESTION_ENGINE_CORPUS", str(CHECKOUT / "corpus"))).expanduser().resolve()
MODEL = "/tmp/lid176.ftz"
TABLE_PATH = "/tmp/fp_th_values.json"
GLOBAL_FLOOR = 0.65

TABLE = json.load(open(TABLE_PATH))
engine = PymupdfTextEngine()
identifier = FastTextLanguageIdentifier(FastTextModel(MODEL))
global_policy = LanguagePolicy(min_confidence=GLOBAL_FLOOR)
any_floor = LanguagePolicy(min_confidence=0.0)
publish = PublishPolicy()


def sha256(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def key_for(language: str) -> str:
    """FinePDFs keys are language_script; lid.176 names no script. A unique prefix match is the key."""
    matches = [k for k in TABLE if k.startswith(language + "_")]
    if len(matches) == 1:
        return matches[0]
    if language in TABLE:  # no script-suffixed entry, but an exact one
        return language
    return ""


def per_language_page_policy(tags):
    """P1: trust a page iff its confidence clears the threshold of the language the page was named."""
    trusted = []
    fallbacks = 0
    for tag in tags:
        key = key_for(tag.language)
        floor = TABLE.get(key, GLOBAL_FLOOR) if key else GLOBAL_FLOOR
        if not key:
            fallbacks += 1
        if tag.confidence >= floor and tag.language != "unknown":
            trusted.append(tag)
    if not trusted:
        return "unknown", 0.0, (), fallbacks
    counts = Counter(t.language for t in trusted)
    language, _ = counts.most_common(1)[0]
    accepted = tuple(t.page_number for t in trusted if t.language == language)
    confidence = sum(t.confidence for t in trusted if t.language == language) / len(accepted)
    return language, confidence, accepted, fallbacks


def main() -> None:
    print("corpus:", CORPUS)
    print("manifest:", json.load(open(os.path.join(CORPUS, "manifest.json")))["crawl_id"],
          json.load(open(os.path.join(CORPUS, "manifest.json")))["domain"])
    print("model:", MODEL, "sha256", sha256(MODEL))
    print("table:", TABLE_PATH, "entries", len(TABLE), "sha256", sha256(TABLE_PATH))
    print("global floor:", GLOBAL_FLOOR)
    print()

    changes_global_drop_perlang_keep = 0
    changes_global_keep_perlang_drop = 0
    page_trust_changes = 0
    fallback_pages = 0
    rows = []

    manifest = json.load(open(os.path.join(CORPUS, "manifest.json")))
    for entry in manifest["entries"]:
        content = open(os.path.join(CORPUS, entry["file"]), "rb").read()
        extracted = engine.extract(Document(name=entry["url"], content=content))
        pages = text_rules.CleaningRules.for_native_text().clean(extracted.pages)
        tags = [identifier.identify(p.text, p.number) for p in pages]

        g = global_policy.apply(tags, len(pages))
        g_ok, g_reason = publish.judge(pages, g)

        p1_lang, p1_conf, p1_accepted, p1_fallbacks = per_language_page_policy(tags)
        p1_outcome = type(g)(p1_lang, p1_accepted, (), p1_conf)
        p1_ok, p1_reason = publish.judge(pages, p1_outcome)

        lo = any_floor.apply(tags, len(pages))
        key = key_for(lo.language)
        t = TABLE.get(key, GLOBAL_FLOOR) if key else GLOBAL_FLOOR
        p2_ok = bool(lo.language != "unknown" and lo.confidence >= t)
        p2_ok = publish.judge(pages, lo)[0] and p2_ok if key else publish.judge(pages, g)[0]

        trusted_global = set(g.accepted_pages)
        trusted_p1 = set(p1_accepted)
        page_trust_changes += len(trusted_global ^ trusted_p1)
        fallback_pages += p1_fallbacks

        if (not g_ok) and p1_ok:
            changes_global_drop_perlang_keep += 1
        if g_ok and not p1_ok:
            changes_global_keep_perlang_drop += 1

        rows.append((entry["url"].rsplit("/", 1)[-1], len(pages), g, g_ok, key, t, p1_ok, p2_ok,
                     len(trusted_global), len(trusted_p1), len(trusted_p1 - trusted_global),
                     len(trusted_global - trusted_p1)))

    print(f"{'document':<58} {'pg':>3} {'global':>10} {'conf':>6} {'key':>9} {'T':>6} "
          f"{'G':>2} {'P1':>3} {'P2':>3} {'trG':>4} {'trP1':>5} {'+':>3} {'-':>3}")
    for (name, n, g, g_ok, key, t, p1_ok, p2_ok, tg, tp1, plus, minus) in rows:
        print(f"{name[:57]:<58} {n:>3} {g.language:>10} {g.confidence:>6.3f} {key or '-':>9} "
              f"{t:>6.3f} {'P' if g_ok else 'D':>2} {'P' if p1_ok else 'D':>3} {'P' if p2_ok else 'D':>3} "
              f"{tg:>4} {tp1:>5} {plus:>3} {minus:>3}")

    print()
    print("documents:", len(rows))
    print("global decisions: published", sum(1 for r in rows if r[3]),
          "dropped", sum(1 for r in rows if not r[3]))
    print("global-drop / per-language-keep:", changes_global_drop_perlang_keep)
    print("global-keep / per-language-drop:", changes_global_keep_perlang_drop)
    print("P2 disagreements with P1:", sum(1 for r in rows if r[6] != r[7]))
    print("page-level trust changes (accepted set differs):", page_trust_changes)
    print("pages with no table entry (fell back to", GLOBAL_FLOOR, "):", fallback_pages)
    print()
    values = list(TABLE.values())
    print("table values <= global floor:", sum(1 for v in values if v <= GLOBAL_FLOOR),
          "> global floor:", sum(1 for v in values if v > GLOBAL_FLOOR),
          "> 0.9:", sum(1 for v in values if v > 0.9))
    print("eng_Latn", TABLE.get("eng_Latn"), "spa_Latn", TABLE.get("spa_Latn"))


if __name__ == "__main__":
    main()
```
