# Is any capture in the committed corpus truncated? — measured, and the answer is no

*Ticket 10, first checkbox: "Measure first: how many captures in the committed NOAA corpus are
truncated? If the answer is 'none', this ticket stays parked and that measurement is the
deliverable." Measured against the corpus on this machine (12 files, 14.93 MiB) and against the WARC
records themselves on `data.commoncrawl.org`, not inferred from the manifest alone.*

## The answer, up front

**0 of 12 captures are truncated, and 0 of 12 documents are partial.** Every file on disk is
byte-identical to the payload Common Crawl recorded and hashed for it. No truncation is observable in
this corpus, by any of four independent witnesses. **Ticket 10 stays parked**, with the caveat in
"N = 12" below: this rules out a truncation rate above ~22%, not a rate of 1%.

*What this branch ships is the narrow half of the ticket: `warc.py` now refuses a record Common Crawl
marked `WARC-Truncated`. What stays parked is the refetch-and-recover machinery. See "What is already
loud, and the one change made" below.*

## The hypothesis in the ticket is wrong: what `bytes` and `length` actually mean

The ticket suggests `bytes` (payload) vs `length` (WARC record) is a truncation signal. Read the code
and it is not, and the two fields are not even in the same units:

| Field | Where it comes from | What it measures |
|---|---|---|
| `bytes` | `fetch.py:95`, `bytes=len(payload)` | Payload bytes the fetcher accepted and wrote. It equals the record's HTTP `content-length` (`warc.py:_http_body` returns exactly the declared length and **raises** if the body carries fewer). |
| `length` | `cdx.py:60`, `int(row["length"])` from the CDX index row | The size of the record **in the `.warc.gz` file** — i.e. *compressed*, and it includes the WARC and HTTP headers. `crawl.py:byte_range` uses it to build the range request. |

Two consequences, both verified below:

- `bytes` vs `length` compares an uncompressed payload against a compressed record. `length < bytes`
  is the normal, healthy case (PDFs are already Flate-compressed, so gzip barely bites), not a
  truncation. All 12 entries have `length / bytes` between 0.777 and 0.991. Reading anything into that
  ratio would be a false positive every time.
- `bytes` is *derived from the bytes that are on disk*: the manifest records what the fetcher
  accepted. A manifest-vs-disk comparison therefore cannot testify about capture-time truncation at
  all — it is a local integrity check on the corpus copy, and only that.

## What the corpus alone can and cannot record

**Observable from `manifest.json` + `pdfs/`:**

- Whether the bytes on disk still match what was written (`bytes`, `digest`) — a post-fetch corruption
  or bad-copy check.
- The payload digest (SHA-256) per URL, and the crawl coordinates needed to re-read the record.

**Not observable from the corpus:**

- Whether Common Crawl itself cut the record short. The only witness for that is the
  `WARC-Truncated` header **inside the WARC record**, which the manifest does not carry. Worse, the
  parser read that header block and threw it away (`warc.py` passed the WARC headers to `_`), so the
  distinction was not merely unrecorded — it was undetectable from anything the corpus kept. That is
  the gap the one code change below closes.
- Why a record is missing. `fetch_corpus` prints exclusions in its `FetchReport` and persists nothing;
  the manifest has no exclusion list. A corpus cannot be audited for what it dropped.
- Anything about rate. 12 entries cannot say what happens on the 13th.

## Measurement, part 1: the corpus as it stands

Every entry, `entry.bytes` against the file on disk (`os.path.getsize`):

| n | sum(`bytes`) | sum(disk) | disk == `bytes` | digest == filename | unique urls |
|---|---|---|---|---|---|
| 12 | 15,649,650 | 15,649,650 | 12 / 12 | 12 / 12 | 12 |

Zero disagreement, and every file's SHA-256 matches the digest it is named by. The corpus is intact as
committed.

Structural completeness, all 12 files:

| Witness | Result |
|---|---|
| Starts with `%PDF` | 12 / 12 |
| Ends with `%%EOF` | 12 / 12 (0–2 bytes follow it — nothing, `\r`, `\n`, or `\r\n`, the PDF's own line ending, not WARC framing) |
| Opens in PyMuPDF without repair (`is_repaired == False`) | 12 / 12 |
| Page count | 4, 10, 11, 11, 12, 15, 16, 18, 25, 41, 45, 84 |
| Linearization dict `/L` equals payload size | 4 / 12 — **discarded as a witness**, see below |

`/L` in the linearization dictionary is not trustworthy: `a3ba26a4fe8a2601` declares 2,172,093 and is
2,178,283 bytes, `7a8e628778f1f44d` declares 2,385,032 and is 3,272,112. A stale `/L` is a normal
producer artifact (linearize, then append), and both of those files are longer than declared — extra
bytes, not missing ones. Reported for honesty, not used.

## Measurement, part 2: the WARC records themselves

ADRs say the corpus is not committed but is reproducible from the manifest and the fetch command
(`docs/adr/0002`). So the records are reachable: 12 range requests, 14.5 MiB, built from
`(warc_filename, offset, length)` exactly as `CrawlRecord.byte_range` does. Each record was
gunzipped and its headers inspected:

| Witness | Result | What it proves |
|---|---|---|
| `WARC-Truncated` header present | **0 / 12** | Common Crawl did not mark any of these records as cut short |
| WARC `Content-Length` == HTTP header block + payload | 12 / 12 (header blocks 595–603 bytes) | The record contains the whole response, headers and all; nothing is missing off the end |
| Trailing WARC framing == exactly 4 bytes (`\r\n\r\n`) | 12 / 12 | The record ends where it says it ends |
| `WARC-Payload-Digest` (sha1, base32) == sha1 of the payload | **12 / 12** | The file on disk is byte-identical to the payload Common Crawl itself hashed — not just the same size |
| `transfer-encoding: chunked` on the captured response | 0 / 12 | The `content-length` check was meaningful for every one |

The payload-digest check is the decisive one: it ties our corpus file to a checksum written by the
upstream capture, so it tests more than self-consistency.

## N = 12 is small, and this measurement does not answer the ticket

- Observed truncations: 0 / 12. The one-sided 95% upper bound on the truncation rate is
  `1 − 0.05^(1/12) = 22.1%`. This measurement excludes a truncation rate above roughly one in five;
  it says nothing about 1% or 5%.
- To bound a rate at ≤1% with 95% confidence needs n ≈ 300 (rule of three). No conclusion about the
  crawl as a whole can be drawn from 12 records.
- The 12 are not survivors with a hidden failure list: `README.md` records the run as
  `fetch-corpus --domain noaa.gov --limit 12`, and the live index returns **50** rows for
  `noaa.gov` / `application/pdf` / `status 200` in `CC-MAIN-2026-39`. These 12 are the head of a
  4×-larger population. The sample was chosen, not exhausted.
- A real answer needs one of: the rest of that 50, a larger multi-domain corpus, or a population drawn
  to be truncation-prone. A refetch is *not* needed — the whole 50 costs ~50 range requests. What a
  conclusion requires is **n**, not new machinery.

## Decision: park ticket 10

**Parked.** The numbers that justify it: 0 / 12 records marked `WARC-Truncated`; 12 / 12 payloads whose
SHA-1 matches the checksum Common Crawl recorded; 12 / 12 structurally complete PDFs; 0 observable
partial documents. There is nothing to recover in this corpus, so refetch-and-recover machinery would
be built against a population with no known members. A truncation today also already fails loudly
rather than silently (see below), so the cost of parking is a hypothetical yield loss, not a
correctness risk.

**The exact measurement that would change the answer:** run the record-header probe in the appendix
over the full 50-row `noaa.gov` population (or n ≥ 300 across domains/crawls) and find **at least one
record with a non-empty `WARC-Truncated` header** — or a payload whose `WARC-Payload-Digest` does not
match its body. That converts a hypothetical into a rate and unblocks ticket 10. If n ≥ 300 still
shows zero, the ticket should be closed rather than parked, and the "truncated captures" concern
retired.

## What is already loud, and the one change made

Read as they stand, the existing strictness already covers most of the ticket's second and fourth
checkboxes:

- A record whose body is shorter than its own `content-length` raises `WarcParseError`
  ("the record declares N payload bytes and carries M") — loud, per-record.
- A gzip member that stops mid-stream, a range answered with the whole shard, a range of the wrong
  size, a non-PDF payload: all raise.
- `fetch_corpus` turns any exception into a `FetchFailure` for that URL only, and never stores a
  partial file (`_store` writes to `.pdf.tmp` then `os.replace`).

The one hole the measurement found and this pass closed: the `WARC-Truncated` header was parsed and
discarded, so a record Common Crawl cut short at capture time *and* whose payload still satisfied its
own `content-length` (possible for a chunked capture) would have passed as a whole document. `warc.py`
now refuses such a record up front:

```
Common Crawl truncated this record at capture time (WARC-Truncated: length)
```

Test first (`tests/infrastructure/test_warc_record.py::test_a_record_common_crawl_marked_truncated_is_rejected`),
red before green: the same record parsed silently before the change. No other code was touched.
Refetch machinery is out of scope for this pass, as the ticket says.

**Found here, fixed by ticket 12:** while probing, the data host cut *our* transfer of the
first record short — `http.client.IncompleteRead: IncompleteRead(1363221 bytes read, 599744 more
expected)`. It failed loudly (a `FetchFailure`, no partial document), but it was **not retried**:
`IncompleteRead` is an `http.client.HTTPException`, not an `OSError`, so it slipped past `_read`'s
`except (urllib.error.URLError, TimeoutError, OSError)`. A transient mid-transfer cut cost a record
instead of a retry. That is a yield bug in the fetch path, not a truncation-detection bug.
`IncompleteRead` now joins that tuple, so a cut is retried with the same attempts and backoff as the
other transport failures; a cut that persists past them is still the loud `FetchFailure` measured here.

## Reproducing the WARC probe

Read-only, no writes to the corpus, one range request per record, ~14.5 MiB for this corpus. Run from
anywhere with the project on `sys.path`; point `CORPUS` at the corpus directory.

```python
import base64, hashlib, json, urllib.request, zlib

CORPUS = "/home/ubuntu/ingestion-engine/corpus"
HDR = b"\r\n\r\n"

def headers(block: bytes) -> dict[str, str]:
    return {
        line.partition(b":")[0].strip().decode().lower(): line.partition(b":")[2].strip().decode()
        for line in block.split(b"\r\n")[1:]
        if b":" in line
    }

for entry in json.load(open(f"{CORPUS}/manifest.json"))["entries"]:
    start = entry["offset"]
    end = start + entry["length"] - 1
    request = urllib.request.Request(
        "https://data.commoncrawl.org/" + entry["warc_filename"],
        headers={"User-Agent": "finepdfs-cocoindex/0.1 (research)", "Range": f"bytes={start}-{end}"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        raw = response.read()
    assert len(raw) == entry["length"], "the range came back the wrong size"
    record = zlib.decompressobj(zlib.MAX_WBITS | 16).decompress(raw)

    warc = headers(record[: record.find(HDR)])
    rest = record[record.find(HDR) + 4 :]
    http_block = rest[: int(warc["content-length"])]
    body = http_block[http_block.find(HDR) + 4 :]

    print(
        entry["file"].split("/")[-1][:16],
        "truncated=", warc.get("warc-truncated", "no"),
        "warc_carries=headers+payload:", int(warc["content-length"]) == http_block.find(HDR) + 4 + len(body),
        "framing=", len(rest) - int(warc["content-length"]),
        "payload_digest_ok=",
        base64.b32encode(hashlib.sha1(body).digest()).decode()
        == warc["warc-payload-digest"].split(":")[1],
        "disk_eq_payload=", open(f"{CORPUS}/{entry['file']}", "rb").read() == body,
    )
```

A record is truncated at capture time when `truncated=` is anything but `no`. On this corpus the
expected output is 12 lines, every one of them `truncated= no`, `warc_carries=headers+payload: True`,
`framing= 4`, `payload_digest_ok= True`, `disk_eq_payload= True`.
