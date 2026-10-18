# Pinned demo Corpus: 19 real Crawl Records, 18 distinct Documents

This is the fixed input for the PDF workflow demo. It is a manifest of 19 real Common Crawl Crawl
Records, restorable byte-for-byte from pinned crawl ranges. It is **not** corpus-scale retrieval
evidence; it is a small, deterministic set on which routing, real OCR, cleaning, exclusions,
duplicate decisions and incrementality can be demonstrated and inspected.

- **Manifest:** [`pdf-demo-corpus-manifest.json`](pdf-demo-corpus-manifest.json)
- **Crawl:** `CC-MAIN-2026-39`
- **Records / Documents:** 19 Crawl Records, 18 distinct content digests
- **Pages / bytes:** 325 pages, 27,116,937 PDF bytes
- **Committed:** the manifest only. PDF bytes, extracted text and model weights are not in the repo.

A **Document** is identified by the digest of its bytes (see the glossary). One pair below is two
Crawl Records for the same Document, which is what exact duplication means; the byte total counts its
bytes twice because the manifest holds two records.

Each entry is a Crawl Record plus the content identity observed when it was fetched: `url`,
`crawl_id`, `warc_filename`, `offset`, `length`, the SHA-256 content `digest`, `bytes` and the
digest-named `file`. The manifest uses the same shape as the corpus `manifest.json` the pipeline
reads, so restoring it is a matter of replaying these entries.

## Composition

| Source | Records | Documents | Pages | What it contributes |
| --- | ---: | ---: | ---: | --- |
| `www.noaa.gov` | 12 | 12 | 292 | Existing corpus: embedded-text-layer Documents, mostly English plus one Spanish newsletter |
| `www.aoml.noaa.gov` | 3 | 3 | 9 | Two genuine image-only scans and one OCR-layered Document for provenance context |
| `buoybay.noaa.gov` (three hosts) | 4 | 3 | 24 | One real exact-duplicate pair (2 records, 1 Document) and one real near-duplicate pair (2 Documents) |
| **Total** | **19** | **18** | **325** | |

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
| `s_geography_text.pdf` (two URLs, one digest) | `5e4c1b36e994` | 18,055 | 2 | 3,506 | 1 |
| `NCBOWinter202324SeasonalSummary.pdf` | `b148a89e002e` | 1,643,393 | 11 | 13,122 | 1-10 |
| `NCBOWinter202324SeasonalSummaryA.pdf` | `a7e0e9cd056f` | 1,643,485 | 11 | 13,177 | 1-10 |

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
Document. It is Spanish on all 16 pages; it does not provide a mixed-language Document. This manifest
does not establish its language label; that requires the configured language adapter and a real run.

## Corpus coverage: what is real and what is controlled

| Coverage case | In the pinned Corpus? | Evidence and provenance |
| --- | --- | --- |
| Exact duplicate | **Real** | Two Crawl Records with one content digest, reachable from two hosts; both payloads hashed and compared. The flow excludes one as `exact-duplicate`. |
| Near duplicate | **Real** | Two Documents of the same 11-page seasonal summary, different bytes and digests, differing in running headers. Measured MinHash similarity 0.9688; the flow excludes one as `near-duplicate`. |
| Mixed-language Document | **Controlled only** | No real bilingual/mixed Document was found in this crawl slice (see below). `tests/fixtures/mixed_language.pdf` is a constructed English/Spanish/Russian fixture, labelled controlled, and is never presented as extraction-quality evidence. |
| Bad input | **Controlled only** | No pinned record is a bad input: the truncated captures found in the slice cannot be restored. `tests/fixtures/not_a_pdf.pdf` is deliberately invalid bytes, labelled controlled, and drives the failed-Exclusion path. |
| Genuine image-only scan | **Real** | Two AOML dropsonde scans with no text layer (above). This is the input to the real-OCR work, not OCR-quality evidence by itself. |

### The exact-duplicate pair

Both Crawl Records hold the same 18,055-byte PDF, reachable at two real URLs:

- `https://www.buoybay.noaa.gov/sites/default/files/files/s_geography_text.pdf`
- `https://content.buoybay.noaa.gov/sites/default/files/files/s_geography_text.pdf`

The index reports the same CC digest (`RIFQCISHNPQTCXNZQDAETUQLDCNO63UB`, base32 SHA-1) for both,
and the payload parsed out of each pinned WARC range hashes to
`5e4c1b36e9949a148616b4e199fdd12a9fc70643c76f607625da376352d25595` (SHA-256) with 18,055 bytes.
The captures are distinct records, not one row counted twice: they come from different WARC files and
offsets (`.warc.gz` `…-00903` at offset `569071896`, and `…-00617` at offset `177347735`).

The deterministic keeper is the smallest URL, `content.buoybay.noaa.gov`. A real run of the four
buoybay records produced this declared decision:

```json
{"duplicate_of": "https://content.buoybay.noaa.gov/sites/default/files/files/s_geography_text.pdf", "kind": "exact", "similarity": 1.0, "url": "https://www.buoybay.noaa.gov/sites/default/files/files/s_geography_text.pdf"}
```

with the exclusion kind `exact-duplicate` and a reason naming the keeper; only the keeper has a
record and Markdown. The same two hosts
also carry byte-identical captures of `s_js_history_text.pdf` and `s_seasonal_text.pdf`; only one
pair is pinned, so the Corpus stays small.

### The near-duplicate pair

The two Crawl Records are the same Winter 2023-24 seasonal summary, published twice:

- `https://buoybay.noaa.gov/sites/default/files/2024-04/NCBOWinter202324SeasonalSummary.pdf`
  (`b148a89e002e…`, 1,643,393 bytes)
- `https://buoybay.noaa.gov/sites/default/files/2024-05/NCBOWinter202324SeasonalSummaryA.pdf`
  (`a7e0e9cd056f…`, 1,643,485 bytes)

They are 11 pages each and differ only in the running header on each page (`Fall 2023` in the first
capture, `Winter 2023-24` in the second): the raw page text is 13,122 and 13,177 characters, and a
line-level diff changes exactly 11 lines, one per page. Measured on the accepted, cleaned text by the
real pipeline with the default Python/Script wiring:

```json
{"duplicate_of": "https://buoybay.noaa.gov/sites/default/files/2024-04/NCBOWinter202324SeasonalSummary.pdf", "kind": "near", "similarity": 0.9688, "url": "https://buoybay.noaa.gov/sites/default/files/2024-05/NCBOWinter202324SeasonalSummaryA.pdf"}
```

The keeper is again the smallest URL (the April capture); the May capture is declared a
`near-duplicate` with similarity 0.969. As a second measure, `difflib.SequenceMatcher` over the raw
page text gives 0.9946. The decision is deterministic: re-running recomputes the same keeper and the
same score.

### What was searched and not found

- **Exact duplicates elsewhere.** The whole `www.aoml.noaa.gov` PDF listing for this crawl is 171
  records, 12 of them in the dropsonde directory; every one has a distinct index digest, and each
  pinned dropsonde digest appears in that listing only at its pinned URL. A digest-filtered index
  query on a pinned dropsonde digest returned that same single row. The AOML dropsonde directory
  therefore contributes no exact duplicate.
- **Near duplicates in the old set.** The 12 NOAA Documents have no pair above the 0.8 similarity
  threshold; the highest is 0.047 (the 2021 Summer and Fall English newsletters). The AOML
  dropsonde logs carry no text layer, so they are not text near-duplicates; empty text fingerprints
  are degenerate and are not used as near-duplicate evidence.
- **Mixed-language Documents.** All 297 PDF records in the `noaa.gov` domain listing for this crawl
  (12 hosts) were surveyed by URL for translated or bilingual candidates, and every Spanish-,
  French- or Puerto-Rico-named record that could be restored was fetched and checked page by page
  with a word-frequency check. No page showed a language different from the rest of its Document:
  the NOAA Caribbean newsletter editions are separate English and Spanish Documents, each in one
  language on every page. No Document with pages in two natural languages was found in this slice,
  so mixed-language coverage is a controlled fixture (below), not a real Corpus claim. Under the
  default `script` Language Mode even a real English/Spanish Document would carry `Latn` on every
  page; naming the languages needs a different Language Mode.
- **Bad input.** Five sampled records in this slice are truncated at capture time
  (`WARC-Truncated: length`): `NOAA-Carib-Newsletter_volume-1-issue-1.pdf`,
  `April-June-keynotes-2026_508-Compliant.pdf`,
  `NOAA-in-the-Caribbean-Winter-2023-Newsletter-English.pdf`,
  `NOAA-in-the-Caribbean-Spring-2023-Newsletter-Spanish.pdf` and
  `Spanish - NOAA in Caribbean Newsletter - SUMMER.pdf`. `restore-corpus` refuses their payloads, so
  they cannot be pinned; a real capture that is complete but not a readable PDF was not found among
  the sampled records. Bad input coverage is therefore a controlled fixture, not a Corpus claim.

## Controlled fixtures

The two controlled fixtures live in `tests/fixtures/` and are labelled in
[`tests/fixtures/README.md`](../../tests/fixtures/README.md). They are constructed, not crawled, and
they must not be cited as evidence of extraction quality on real PDFs.

| Fixture | Provenance | What the offline test shows |
| --- | --- | --- |
| `not_a_pdf.pdf` | Deliberately invalid bytes: a `%PDF-1.7` header followed by prose and no body or xref. | The flow records that Document as a failed Exclusion while its neighbours complete. |
| `mixed_language.pdf` | Constructed 3-page PDF: English, Spanish (both Latin script) and Russian (Cyrillic), generated with PyMuPDF and a subset DejaVu Sans. | Under the default `script` mode the two Latin pages are `Latn` and the Cyrillic page is dropped as the minority script; English and Spanish are not separated. |

The tests are in `tests/infrastructure/test_flow_end_to_end.py`, at the same CocoIndex flow seam the
acceptance tests use. Neither fixture proves anything about OCR, and neither is in the manifest.

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
verifies the payload before storing it. The exact-duplicate pair shares one digest-named file, so the
second record is already present after the first is stored. Expected output:

```text
restoring 19 pinned PDFs from CC-MAIN-2026-39 into corpus/pdf-demo
records seen 19, fetched 18, already present 1, failed 0
```

Run it again; verified local bytes are not fetched again:

```text
restoring 19 pinned PDFs from CC-MAIN-2026-39 into corpus/pdf-demo
records seen 19, fetched 0, already present 19, failed 0
```

Then inspect the corpus the pipeline will use:

```bash
.venv/bin/finepdfs inspect --corpus corpus/pdf-demo
```

Expected summary: `documents: 19`, `distinct content: 18`, `bytes: 27116937`.

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
print(f"verified {len(manifest['entries'])} records and {len({e['digest'] for e in manifest['entries']})} documents")
PY
```

`restore-corpus` never calls the Common Crawl index: it turns each pinned entry into a `CrawlRecord`
and calls the existing `CrawlSource.fetch`, which issues the ranged WARC read and parses the record.
The index is only consulted by `fetch-corpus`, which discovers a new corpus rather than replaying
this one.

## Replay the duplicate decisions

After restoring, run the whole Corpus with the free text-layer engine and read the declared duplicate
report:

```bash
FINEPDFS_DB=/tmp/pdf-demo.db FINEPDFS_CORPUS=corpus/pdf-demo FINEPDFS_OUT=/tmp/pdf-demo-out \
  PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli run
cat /tmp/pdf-demo-out/dedupe/duplicates.jsonl
```

A run on 2026-10-18 printed `recorded 15 published documents, 0 failed documents and 4 other
exclusions`. The two duplicates above appear in `dedupe/duplicates.jsonl`; `dedupe/kept.jsonl` names
their keepers. The other two exclusions are the genuine image-only AOML scans dropped with the
text-layer engine, because it reads no text from them. Routing them to a real OCR Engine and checking
its output is separate work; this run does not do it.

## Failure behavior

A missing WARC object, a rejected range request, a transport failure, or a payload whose size or
SHA-256 is not the pinned one prints a `failed: <url> (<reason>)` line and exits 1. The bytes of a
mismatched payload are not stored.

The pinned entries are written to `corpus/pdf-demo/manifest.json` before fetching starts. A record
that could not be restored therefore stays declared. A later `run` tries to read it, records a failed
Exclusion for that Document, and does not treat the missing file as an intentional source deletion.
Removing a Crawl Record from the manifest is the only thing that removes its declared outputs.

## How this manifest was verified

On 2026-10-18 every entry was checked against independent sources:

1. **Local bytes.** The SHA-256 of the locally retained PDF matched the manifest `digest`, and its
   size matched `bytes`.
2. **The live index.** An exact-URL query to the `CC-MAIN-2026-39` index returned one row whose
   `filename`, `offset` and `length` matched the manifest. The index's own base32 SHA-1 digest
   matched a SHA-1 of the local payload. For the exact-duplicate pair, the `noaa.gov` domain listing
   returned both URLs with the same index digest, and both parsed payloads had the same SHA-256.
3. **A fresh ranged capture.** `https://data.commoncrawl.org/<warc_filename>` answered `206` for the
   pinned inclusive byte range, and parsing that one WARC record produced a PDF whose SHA-256 and
   byte count matched the manifest. For the three AOML records the fetched WARC slice was also
   byte-for-byte identical to the separately downloaded `.warc.gz` capture.
4. **The duplicate decisions.** A real flow run over the restored Corpus declared one
   `exact-duplicate` and one `near-duplicate` (similarity 0.9688) with the keepers named above.

No digest in the manifest was inferred from a URL or a file name; each is the SHA-256 of the payload
that was parsed out of the pinned WARC range.

## What this does and does not demonstrate

The Corpus is 19 Crawl Records covering 18 Documents, 325 pages and 27,116,937 bytes. On a fixed
input that size, it demonstrates:

- 19 real crawled PDFs can be restored byte-for-byte from pinned Crawl Records without asking for
  the latest crawl;
- the input includes two genuine image-only scans with known page counts, plus native-text Documents;
- real exact and near duplicate decisions, with deterministic keepers, on real crawled documents
  (one exact pair sharing one digest at two URLs, one near pair at 0.9688 similarity);
- the input is reproducible from the manifest alone, with no PDF bytes or model weights in git.

It does **not** demonstrate:

- retrieval quality, ranking, or any corpus-scale claim: this is about 18 Documents;
- OCR quality, OCR page mapping, or which engine should be used — that needs a real Engine run and
  manually checked output;
- a verified language label for the Spanish Document;
- a real mixed-language Document: that case is a controlled fixture only;
- a real bad input: the truncated captures in this slice cannot be restored, so that case is a
  controlled fixture only;
- permanent availability of the captures. If Common Crawl removes a WARC object, the restore fails
  visibly; retaining a local corpus copy is the way to keep the demo independent of that.

## Licence and attribution

The PDFs are Common Crawl captures of documents published by NOAA and its programs. Common Crawl's
terms of use apply to the crawl data, and the source sites' terms apply to the documents. The
repository commits pointers (URLs, WARC ranges, digests), not the PDF bytes.
