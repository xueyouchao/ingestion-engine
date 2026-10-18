# Pinned demo Corpus: 15 real Common Crawl PDFs

This is the fixed input for the PDF workflow demo. It is a manifest of 15 real Common Crawl
Documents, restorable byte-for-byte from pinned Crawl Records. It is **not** corpus-scale retrieval
evidence; it is a small, deterministic set on which routing, real OCR, cleaning, exclusions and
incrementality can be demonstrated and inspected.

- **Manifest:** [`pdf-demo-corpus-manifest.json`](pdf-demo-corpus-manifest.json)
- **Crawl:** `CC-MAIN-2026-39`
- **Documents:** 15, with 15 distinct content digests
- **Pages / bytes:** 301 pages, 23,793,949 PDF bytes
- **Committed:** the manifest only. PDF bytes, extracted text and model weights are not in the repo.

Each entry is a Crawl Record plus the content identity observed when it was fetched: `url`,
`crawl_id`, `warc_filename`, `offset`, `length`, the SHA-256 content `digest`, `bytes` and the
digest-named `file`. The manifest uses the same shape as the corpus `manifest.json` the pipeline
reads, so restoring it is a matter of replaying these entries.

## Composition

| Source | Documents | Pages | What it contributes |
| --- | ---: | ---: | --- |
| `www.noaa.gov` | 12 | 292 | Existing corpus: embedded-text-layer Documents, mostly English plus one Spanish newsletter |
| `www.aoml.noaa.gov` | 3 | 9 | Two genuine image-only scans and one OCR-layered Document for provenance context |
| **Total** | **15** | **301** | |

Measured with PyMuPDF while verifying the captures. "Text" is stripped embedded text characters,
summed over all pages; it says nothing about OCR quality.

| Document | SHA-256 (first 12) | Bytes | Pages | Text | Images/page |
| --- | --- | ---: | ---: | ---: | ---: |
| `NOAA-in-the-Caribbean-2021-Summer-Newsletter-English.pdf` | `0cf0b893f7d9` | 1,983,254 | 11 | 18,115 | 0-2 |
| `Additional-Information-NOAA-tribal-provisions-in-IIJA.pdf` | `c6e326f5a929` | 157,647 | 4 | 8,217 | 0 |
| `NOAA-in-the-Caribbean-Fall-2021-Newsletter-English.pdf` | `c1615cbde4ed` | 1,979,699 | 11 | 20,993 | 1-3 |
| `AwardActionRequestGuidance_MAR2022B.pdf` | `0f5bc7a46eb9` | 875,711 | 41 | 51,024 | 0-10 |
| `NOAA_en_el_Caribe_Edicion_Verano_2022_Espanol.pdf` | `90c835de782a` | 1,577,332 | 16 | 29,753 | 0-1 |
| `NOAA_in_the_Caribbean_Summer_2022_Newsletter_English.pdf` | `81a89f1c302d` | 1,134,780 | 15 | 26,543 | 0-1 |
| `CELC_2022_Educators_Workshop-Summary_Report.pdf` | `a3ba26a4fe8a` | 2,178,283 | 25 | 37,739 | 0-2 |
| `Facilitators-Guide-to-MWEE-Training-2023.pdf` | `7a8e628778f1` | 3,272,112 | 84 | 93,128 | 0-1 |
| `NOAA-in-the-Caribbean-Autumn-2022-Newsletter_English_Final.pdf` | `c6270b7e13f8` | 1,467,791 | 18 | 30,863 | 0-2 |
| `IIJATribalProvisionsNOAAExecutiveSummaryandResponse.pdf` | `ae1650f38888` | 230,857 | 12 | 28,980 | 0-1 |
| `IRATribalNOAAExecutiveSummaryandResponse_0.pdf` | `a3b0be2d1ffa` | 314,041 | 10 | 19,663 | 0-1 |
| `NOAA_Tribal_Consultation_Handbook%202023_FINAL.pdf` | `6dd89aa9c604` | 478,143 | 45 | 125,518 | 0-2 |
| `20090827I1droplog.pdf` | `459716fc0351` | 834,848 | 1 | 0 | 1 |
| `19950826I1_FDIR.PDF` | `efda8b605525` | 4,597,124 | 6 | 6,756 | 1 |
| `20090826I2droplog.pdf` | `b5f7ac05c298` | 2,712,327 | 2 | 0 | 1 |

The three AOML records are:

- `459716fc0351dd1ab374eda66f152bc32116387e95c7a6437ad7382c6844db52` — one page, no embedded
  text, one image. Adobe Acrobat Image Conversion Plug-in produced it. This is the genuine
  image-only scan.
- `b5f7ac05c298f1a0db0fff96325b0ab51fb5050de0b03f65001c2c2a3a20265f` — two pages, no embedded
  text, one image per page, same producer. A second genuine image-only scan, useful because the
  original page numbers are unambiguous.
- `efda8b60552528173c02784b108b40b5ec113e0903ef66ff2ba2fa51ca8331a9` — six pages, one image per
  page, with an existing text layer created by `ocrmypdf 13.4.3 / Tesseract OCR-PDF
  v5.2.0.20220712`. It is **not** a genuine scan for OCR-quality evidence; keep it as provenance
  context only.

The Spanish newsletter, `NOAA_en_el_Caribe_Edicion_Verano_2022_Espanol.pdf`, is a real non-English
Document. This manifest does not establish its language label; that requires the configured language
adapter and a real run.

## Restore

From the repository root, with the project's virtual environment installed:

```bash
.venv/bin/finepdfs restore-corpus \
  --manifest docs/demo/pdf-demo-corpus-manifest.json \
  --corpus corpus/pdf-demo
```

If the console script's shebang names an interpreter that is not on this machine, use the module
entry point instead:

```bash
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli restore-corpus \
  --manifest docs/demo/pdf-demo-corpus-manifest.json \
  --corpus corpus/pdf-demo
```

A fresh restore fetches every pinned record with one ranged HTTP request into the WARC shard and
verifies the payload before storing it. Expected output:

```text
restoring 15 pinned PDFs from CC-MAIN-2026-39 into corpus/pdf-demo
records seen 15, fetched 15, already present 0, failed 0
```

Run it again; verified local bytes are not fetched again:

```text
restoring 15 pinned PDFs from CC-MAIN-2026-39 into corpus/pdf-demo
records seen 15, fetched 0, already present 15, failed 0
```

Then inspect the corpus the pipeline will use:

```bash
.venv/bin/finepdfs inspect --corpus corpus/pdf-demo
```

Expected summary: `documents: 15`, `distinct content: 15`, `bytes: 23793949`.

To check the bytes independently of the restore command:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import hashlib, json
from pathlib import Path

manifest = json.loads(Path("docs/demo/pdf-demo-corpus-manifest.json").read_text())
for entry in manifest["entries"]:
    path = Path("corpus/pdf-demo") / entry["file"]
    assert path.stat().st_size == entry["bytes"], entry["url"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["digest"], entry["url"]
print(f"verified {len(manifest['entries'])} documents")
PY
```

`restore-corpus` never calls the Common Crawl index: it turns each pinned entry into a `CrawlRecord`
and calls the existing `CrawlSource.fetch`, which issues the ranged WARC read and parses the record.
The index is only consulted by `fetch-corpus`, which discovers a new corpus rather than replaying
this one.

## Failure behavior

A missing WARC object, a rejected range request, a transport failure, or a payload whose size or
SHA-256 is not the pinned one prints a `failed: <url> (<reason>)` line and exits 1. The bytes of a
mismatched payload are not stored.

The pinned entries are written to `corpus/pdf-demo/manifest.json` before fetching starts. A record
that could not be restored therefore stays declared. A later `run` tries to read it, records a failed
Exclusion for that Document, and does not treat the missing file as an intentional source deletion.
Removing a Crawl Record from the manifest is the only thing that removes its declared outputs.

## How this manifest was verified

On 2026-10-18, every one of the 15 entries was checked against three independent sources:

1. **Local bytes.** The SHA-256 of the locally retained PDF matched the manifest `digest`, and its
   size matched `bytes`.
2. **The live index.** An exact-URL query to the `CC-MAIN-2026-39` index returned one row whose
   `filename`, `offset` and `length` matched the manifest. The index's own base32 SHA-1 digest
   matched a SHA-1 of the local payload.
3. **A fresh ranged capture.** `https://data.commoncrawl.org/<warc_filename>` answered `206` for the
   pinned inclusive byte range, and parsing that one WARC record produced a PDF whose SHA-256 and
   byte count matched the manifest. For the three AOML records the fetched WARC slice was also
   byte-for-byte identical to the separately downloaded `.warc.gz` capture.

No digest in the manifest was inferred from a URL or a file name; each is the SHA-256 of the payload
that was parsed out of the pinned WARC range.

## What this does and does not demonstrate

It demonstrates:

- 15 real crawled PDFs can be restored byte-for-byte from pinned Crawl Records without asking for
  the latest crawl.
- The corpus includes two genuine image-only scans with known page counts, plus 12 Documents with
  embedded text, on a fixed and inspectable input.
- The input is reproducible from the manifest alone, with no PDF bytes or model weights in git.

It does **not** demonstrate:

- retrieval quality, ranking, or any corpus-scale claim: this is about 15 Documents;
- OCR quality, OCR page mapping, or which engine should be used — that needs a real Engine run and
  manually checked output;
- a verified language label for the Spanish Document;
- permanent availability of the captures. If Common Crawl removes a WARC object, the restore fails
  visibly; retaining a local corpus copy is the way to keep the demo independent of that.

## Licence and attribution

The PDFs are Common Crawl captures of documents published by NOAA and AOML. Common Crawl's terms of
use apply to the crawl data, and the source sites' terms apply to the documents. The repository
commits pointers (URLs, WARC ranges, digests), not the PDF bytes.
