# The match lands on the page for 310 of 313 pages; the 3 OCR-only scan pages cannot carry a word box

*Ticket 03, "Does the match land on the page image". "Can the page show the matched words highlighted
**on the rendered PDF page itself**, and at what cost?" Everything below was measured on this machine
with the PyMuPDF already in `.venv` (pymupdf 1.28.2 / MuPDF 1.28.2) against the 18 real NOAA PDFs in
`.demo/corpus/pdfs/`, their 325 pages, the 15 pymupdf page exports in `.demo/out/pages/` and the manifest
in `.demo/corpus/manifest.json`. Nothing was installed, no model was run, no repo file was changed
except this note. The Docling JSON shape is cited from `docling-core` source, not from a run: Docling is
not importable in this venv (`ModuleNotFoundError: No module named 'docling'`), and the runbook's
Docling run is not on disk.*

## The answer, up front

| Page class | Pages in the 313 | Word/char boxes on the page? | Highlight on the page image |
|---|---|---|---|
| Native text layer (pymupdf) | 310 | **Yes** — `get_text("words")` / `rawdict` give float boxes in points, top-left origin, y down | **Exact.** 24,000 of 24,000 five-word windows inside a published line re-found by `page.search_for()` (100.00%) |
| Image-only scan, text from Docling OCR (`459716fc03`, 1 p; `b5f7ac05c2`, 2 p) | 3 | **No.** `get_text("words")` is `[]`, `get_text("text")` is `""`, 0 characters in `rawdict`; one full-page 200 dpi JPEG, no text object | **Impossible from the PDF.** The PDF has no text to locate. Only Docling's own JSON carries boxes, and the adapter throws them away (see §5) |
| (Contrast) OCR *text layer* inside the PDF — `efda8b6055`, ocrmypdf/Tesseract | 0 of 313 (it is one of the 310) | **Yes**, Tesseract word boxes | **Approximate but lands on the ink** (17.5–22.2% ink inside the box vs 2.9–4.0% in margin controls) |

Bundle cost, measured over every page and stated for the 313 pages the bundle must ship (bytes on disk,
no delivery compression): **18.1–18.5 MiB at 72 dpi**, **35.2–36.5 MiB at 110 dpi**, **51.9–56.1 MiB at
144 dpi**, for the only three encodings PyMuPDF can produce (PNG colour, PNG greyscale, JPEG). **WebP is
not available at all**: `Pixmap.tobytes()` in this build rejects `webp`, and no Pillow is installed
(§6.1). The coordinate payload itself is cheap — a word box map for the 310 text pages is **1.87 MiB**
(6.3 KiB/page, 25.8 B/word).

The blunt version for ticket 09's size budget: **highlight on the page is a solved problem for 310
pages and a non-problem for 3**; the size budget is decided entirely by dpi and colour, not by the
highlight.

## What the corpus actually is

`docs/demo/pdf-demo-corpus.md` pins 19 Crawl Records / 18 Documents / 325 pages. The 313 in the ticket
are the Docling run's published page records: the 310 pymupdf-run accepted pages plus the 3 pages of the
two image-only scans, with the near-duplicate `a7e0e9cd056f…` (11 pages) excluded by Deduplication.
Confirmed mechanically: `.demo/out/pages/*.jsonl` holds 15 files / 310 records / 501,870 characters, and
the two scans contribute 1 + 2 = 3 pages (`.demo/out/excluded/5861c9f211cace9b.json`,
`…/0ae09935a21c39d4.json`, both dropped as `"0 pages is fewer than the 1 page minimum"` under pymupdf).
All 310 measured pages are `engine: "pymupdf@1.28.2"`, and no record under `.demo/out/` or `out/` names
`docling` — the runbook's Docling exports are not on disk.

Page geometry is remarkably uniform: **316 of 325 pages are US Letter** (314 portrait 612×792 pt, 2
landscape 792×612), 9 pages are odd sizes (the 3 scan pages and the 6 pages of `efda8b6055`), and
**every page in the corpus has `page.rotation == 0`, `cropbox == mediabox`, and a MediaBox origin at
(0, 0)`** — measured, not assumed. Rotation and crop are therefore *rules to implement*, not current hazards.

## 1. What PyMuPDF exposes per page, and in what space

`page.get_text("words")` returns an 8-tuple per word: `(x0, y0, x1, y1, word, block_no, line_no,
word_no)`. First word of `c6e326f5a929…` page 1, on a 612×792 page:

```
(72.0, 122.50701904296875, 527.5441284179688, 135.79608154296875, '----…', 1, 0, 0)
```

`page.get_text("rawdict")` gives one entry per **character**, each with `origin` (baseline point) and
`bbox`, plus `synthetic` and `c`; spans carry `font`, `size`, `flags`, `color`, `ascender`, `descender`,
`bidi`. Measured first char: `{'origin': (139.67, 93.97), 'bbox': (139.67, 81.49, 143.17, 97.00), 'c':
' ', 'synthetic': False}`.

The space is **MuPDF space: floats, points (1/72 in), origin at the top-left of the unrotated page, y
increasing downwards** — [`app3.html#coordinates`](https://pymupdf.readthedocs.io/en/latest/app3.html#coordinates):
"In PDF, the origin (0, 0) of a page is located at its bottom-left point. In MuPDF, the origin (0, 0) of
a page is located at its top-left point … one point equals 1/72 inches" and `Page.transformation_matrix`
converts between the two. Rotation is *not* applied to returned coordinates —
[`page.html`](https://pymupdf.readthedocs.io/en/latest/page.html): "all coordinates returned by methods
and attributes pertain to the unrotated page … the same is true for coordinates returned by
`Page.get_text()` … If you want to find out where an object is located in rotated coordinates, multiply
the coordinates with `Page.rotation_matrix`". Both statements reproduce here exactly (§2).

Corpus scale: 75,999 words, 505,571 characters (whitespace included) across the 310 pages — a
word-level map is ~1/6.6 the size of a character-level one (§6.4).

## 2. Rotation and CropBox: the mapping rule, proved

**Rotation** (synthetic page, single glyph, 144 dpi render; ink fraction measured inside the box):

| `page.rotation` | `page.rect` | pixmap | ink inside raw word box | ink inside `word_box * rotation_matrix` |
|---:|---|---|---:|---:|
| 0 | 400×200 | 800×400 | 0.165 | 0.165 |
| 90 | 200×400 | 400×800 | **0.000** | **0.165** |
| 180 | 400×200 | 800×400 | **0.000** | **0.161** |
| 270 | 200×400 | 400×800 | **0.000** | **0.161** |

So a rendered image is in **rotated** space (`page.rect` follows rotation, and the pixmap is
`rect × dpi/72`), while words are in unrotated space. The one mapping needed:

```
pixel_box = (word_box * page.rotation_matrix) * (dpi / 72)
```

**CropBox**: `set_cropbox(Rect(100, 100, 500, 700))` on a 612×792 page gives `page.rect == (0, 0, 400,
600)`, shifts every word by exactly `(-100, -100)`, clips words outside the crop, and renders a 400×600
pixmap at 72 dpi. Because `get_text` and `get_pixmap` both work in the *cropped* space, no extra offset
is needed — the same formula holds. Neither case occurs in this corpus (all 325 pages are unrotated with
`cropbox == mediabox`), so a demo for *this* corpus needs only `× dpi/72`; the rotation term is one line
that must not be forgotten when the corpus changes.

**Overlay correctness on a real native page** (`c6e326f5a929…` p1, random words, 144 dpi): mean ink
inside the word boxes **0.149**, inside same-sized margin rectangles **0.000**.

## 3. `page.search_for()` is the ready-made matcher, and it is exact within a line

`page.search_for(needle)` returns a list of `Rect` in the same unrotated space (`quads=True` for tilted
text). It is case-insensitive — measured: the word `Additional` is found by `additional`, `ADDITIONAL`
and `Additional`.

Recall, measured over **every** published page of the pymupdf run (310 pages, 24,000 windows made of 5
consecutive tokens taken from inside a published line):

| Windows | Found | Missed |
|---|---:|---:|
| 5-word windows inside one published line | **24,000** | **0 (0.00%)** |
| 7-word windows straddling a published line break | 1,012 of 1,050 | 38 (3.6%) |

The 3.6% is **not** a PyMuPDF limitation: it is our own Cleaning. Worked example, `6dd89aa9c604` p2:
published Page Text reads `… Information to Support the Consultation VI. …` because
`_drop_page_number_lines` (`src/finepdfs/domain/text.py:53`, applied at `:70-71`) removed the standalone
page-number line `8`, and the raw PDF has `Consultation\n8\nVI.`. A phrase that joins across a line the
cleaner deleted cannot exist in the PDF. A second failure mode is a table header the cleaner's
`normalise` (`text.py:33-38`) reflowed into one line (`… Comments & NOAA Response Comments on the …`).
Both are visible in the published text and can be predicted, not just discovered.

One match yields **several rects** (9 for a 5-word phrase), one per word run; their union is slightly
taller than the union of the word boxes (79.23 vs 81.49 top, 97.90 vs 97.00 bottom) because it includes
ascender/descender slack. For a highlighter that is a feature, not a mismatch.

## 4. The two image-only scans: no boxes exist, at all

Measured on both files, every page:

| Document | Pages | `get_text("words")` | `rawdict` characters | `get_text("text")` | Images |
|---|---:|---:|---:|---:|---|
| `459716fc0351…` (20090827I1droplog.pdf, 784×599 pt) | 1 | 0 | 0 | 0 chars | 1 × 2178×1664 JPEG grey, ≈200 dpi |
| `b5f7ac05c298…` (20090826I2droplog.pdf, portrait then landscape) | 2 | 0 | 0 | 0 chars | 1 per page, 1664×2192 / 2192×1664, ≈200 dpi |

There is no invisible text layer, no `/XObject`, no OCR surface: the page is a JPEG and nothing else. A
highlight on the page image is **impossible** without a second source of geometry. `get_pixmap` still
works (that page image renders fine), which is precisely the trap: the *image* is available while the
*words* are not.

These are also the most expensive pages to ship per page (image content defeats PNG): 468,553 bytes/page as
greyscale PNG at 110 dpi versus 119,596 bytes/page for the text pages, a 3.9× multiplier — but in JPEG
at 110 dpi they cost the same as text pages (120,091 vs 117,427 bytes), because a scanned JPEG
re-encodes cheaply (§6.3).

**Contrast case, already inside the 310:** `efda8b6055` is a 300 dpi scan with an *embedded* text layer
written by `ocrmypdf 13.4.3 / Tesseract OCR-PDF` (`docs/demo/pdf-demo-corpus.md`). It behaves like a
native page: `get_text("words")` returns boxes and the ink test lands them on the printed text (0.175 and
0.222 inside the boxes vs 0.029/0.040 in margin controls). So "scan ⇒ no boxes" is false as a rule — the
rule is "no text layer ⇒ no boxes", and Tesseract boxes are approximate at word/segmentation level while
exact in position.

## 5. Docling's JSON does carry boxes — the adapter keeps only the page number

`docling-core`'s model (`docling_core/types/doc/common/reference.py`) is:

```python
class ProvenanceItem(BaseModel):
    page_no: Annotated[int, ...]
    bbox: Annotated[BoundingBox, ...]
    charspan: CharSpan
```

and `BoundingBox` (`docling_core/types/doc/base.py`) is `l, t, r, b` plus `coord_origin`
(`TOPLEFT` / `BOTTOMLEFT`), with `DoclingDocument.pages: dict[int, PageItem]` carrying each page's
`Size`. Table cells additionally carry their own optional `bbox` (visible in `DoclingDocument`'s
`_clamp_table_cell_bboxes`). So Docling JSON has: **page number + item-level box + character span within
the item**, which is block granularity, not word granularity.

`DoclingEngine` uses only the page number:

- `src/finepdfs/infrastructure/engines/docling_engine.py:182-193` — `_page_number()` reads the first
  valid `prov[].page_no` and returns `int | None`; `bbox` and `charspan` are never touched.
- `:140-143` — every item on a page is appended and joined with `"\n\n"` into one `PageText`. Once that
  join happens, the item boundaries that the boxes belonged to are gone.
- `:72-73` — `--image-export-mode placeholder` means the JSON does not even carry page images.

**Nothing in the pipeline retains coordinates anywhere.** `PageText` is `number` + `text`
(`src/finepdfs/domain/document.py:29-33`); `PageRecord` is `doc_id, source_url, digest, page_number,
text, engine` (`src/finepdfs/domain/publishing.py:57-68`); the declared page export is exactly
`record.as_json()` (`src/finepdfs/infrastructure/publishing/disk.py:60-62`). `grep -rn bbox` over
`src/finepdfs/` finds **nothing** (every `rect` hit is the word "directory"), and no source file calls
`rawdict` or `search_for` either.
The only renderer that exists is `PdfDocument.render_page_png(page_number, dpi=200)`
(`src/finepdfs/infrastructure/engines/pdf_document.py:52-54`), and its only caller is the PaddleOCR
adapter (`paddleocr_vl.py:57`, `:94`) — there is no JPEG path and no page-image pipeline to reuse.

Consequence for the 3 scan pages: **today, a page-side highlight is impossible; with the adapter
changed to retain `prov[].bbox`, it becomes block-approximate** — you can box the paragraph / line /
table cell that contains the match, not the words. Getting a word box inside that block would mean
interpolating across `charspan` (approximate, and visibly wrong on the table pages these scans are), or
running a word-level engine instead. And the OCR text itself is partly junk — the runbook records
handwriting read as `12 | Jicok | 61`, `WxwxA Dannyl`, `Presgure`, `Comantg` — so an approximate box may
also enclose words the OCR invented.

## 6. Bytes: what 313 rendered pages cost

Measured on **all 325 pages** of the corpus with `page.get_pixmap(dpi=…, colorspace=…)` then
`.tobytes(fmt)` / `.tobytes("jpg", jpg_quality=q)`; no resampling, no post-processing. The 313-page column
is the measured 314-page set (all pages minus the near-duplicate `a7e0e9cd056f…`, 11 pages) minus one
page of `6dd89aa9c604` (the page the language policy dropped), priced at that document's mean page.

| Encoding | 72 dpi | 110 dpi | 144 dpi |
|---|---:|---:|---:|
| PNG, RGB | 40.8 MiB | 86.3 MiB | 123.2 MiB |
| PNG, greyscale | **18.5 MiB** | 36.5 MiB | 56.1 MiB |
| JPEG q60, RGB | **18.1 MiB** | **35.2 MiB** | **51.9 MiB** |
| JPEG q75, RGB | 22.3 MiB | 42.8 MiB | 62.9 MiB |

Per-page distribution (all 325 pages, so the 3 scan pages are included), bytes per page:

| Encoding | p50 | p90 | max |
|---|---:|---:|---:|
| PNG greyscale @110 | 92,166 | 243,025 | 683,785 |
| JPEG q60 @110 | 119,279 | 161,753 | 199,375 |
| PNG RGB @110 | 197,468 | 654,318 | 2,241,786 |
| PNG RGB @144 | 241,866 | 1,021,066 | 3,764,598 |

### 6.1 WebP is not on the table in this environment

`Pixmap.tobytes("webp")` and `pix.save("x.webp")` both fail with
`Image format webp not in ('png', 'pnm', 'pgm', 'ppm', 'pbm', 'pam', 'tga', 'tpic', 'psd', 'ps', 'jpg',
'jpeg')`; `.venv/bin/pip list` has no Pillow (nor any other image library), and installation is out of
bounds for this ticket. So the ticket's "PNG, WebP, JPEG" question is answered as: **PNG and JPEG are
measurable, WebP is impossible without a new dependency**, and I will not quote a number I could not
produce. If WebP matters, it is a packaging decision (add Pillow, or shell out to `cwebp`), not an
exporter option.

### 6.2 Greyscale is the cheapest real win

Greyscale shrinks PNG 2.2× at every dpi tested and is visually fine for these documents (the only page
images that are not text-on-white are the scans, and those are already 1-channel JPEG). It barely helps JPEG (JPEG already spends
its bits on luma), which is why **PNG greyscale @72 (18.5 MiB) ≈ JPEG q60 @72 (18.1 MiB)** while PNG
greyscale keeps text crisp — no ringing artefacts around glyphs, which matters when the image sits beside
the text and the reader is asked to compare them.

### 6.3 Where the bytes are

- **Text pages** dominate the total only by count. At 110 dpi greyscale PNG: 119,596 B/page average over
  the 322 non-scan pages.
- **Image-heavy newsletters** dominate the tail: p90 243,025 B, max 683,785 B at 110 dpi greyscale.
- **The 3 scan pages** are 468,553 bytes/page at 110 dpi greyscale PNG (1.4 MiB for all three),
  120,091 bytes/page as JPEG.
- **A cheaper alternative to rendering at all**: the whole pinned corpus is 27,116,937 PDF bytes
  (`docs/demo/pdf-demo-corpus.md`). Shipping the PDFs plus a browser PDF renderer is *smaller than any
  ≥110 dpi page-image set*, but it imports a JS dependency and a build step the demo has ruled out, and
  the explorer already serves PDFs (`src/finepdfs/interfaces/explorer.py:211-230`, `/pdf/<digest>.pdf`).

### 6.4 Cost to produce, and the cost of the coordinates

Rendering is not the bottleneck: PNG greyscale at 72 dpi takes ~9 ms/page, at 110 dpi ~14 ms, at 144 dpi
~24 ms; JPEG q60 at 110 dpi ~54 ms. **313 pages of greyscale PNG at 110 dpi is ~4–5 s single-threaded.**

The coordinate payload, measured by serialising `get_text("words")` boxes per page as compact JSON
(0.1 pt rounded floats, boxes only, no text — the browser already has the text):

| Payload | Elements | JSON size | Per page | Per element |
|---|---:|---:|---:|---:|
| Word boxes, 310 text pages | 75,999 words | **1,961,699 B (1.87 MiB)** | 6,328 B | 25.8 B |
| Character boxes (`rawdict`), same pages | 505,571 chars | 13,047,824 B (12.44 MiB) | 42,090 B | 25.8 B |

So a word-level box map costs **~5% of a 36 MiB image bundle**, and a character-level one costs ~34% —
another reason word granularity is the right choice for the demo. For comparison the published text
itself is 609,531 B of page JSONL (501,870 characters) and 781,677 B of chunk JSONL.

## 7. Where coordinates would have to be retained

The static page cannot call PyMuPDF, so any page-side highlight must be **baked at export time** from
coordinates that the pipeline currently discards. Note the constraint that follows: the demo's matcher
runs in the browser (ticket 14), so the *query* is not known when the bundle is written. `search_for` is
therefore the export-time tool for fixed demo queries and for validating a box map; for arbitrary
queries the bundle must ship the word boxes themselves and mark tokens by position. The chain, in the
order the bytes flow:

1. `PdfDocument.page_texts()` — `src/finepdfs/infrastructure/engines/pdf_document.py:45-50` — is the only
   place native text is read, and it calls `get_text("text")` and throws the word list away. A sibling
   `page_words()` (or `page_boxes()`) belongs here, next to the existing `render_page_png`.
2. `PageText` — `src/finepdfs/domain/document.py:29-33` — `number` + `text` only. Adding boxes to it
   would put geometry inside the citation unit that the Markdown projection and every Engine share.
3. `ExtractedDocument` — `src/finepdfs/domain/document.py:47-63` — `pages` + `engine`. A third Engine
   (Docling) produces no word boxes, so any geometry field must be optional and per-engine, or it must
   be a separate structure that only the native path fills.
4. `PageRecord` / the declared export — `src/finepdfs/domain/publishing.py:57-68` and
   `src/finepdfs/infrastructure/publishing/disk.py:60-62`. This is the contract that `accepted_pages`
   reads back (`disk.py:173-199`), so extending it is a declared-output change, not a local edit.
5. `DoclingEngine` — `src/finepdfs/infrastructure/engines/docling_engine.py:115-143`,
   `:182-193` — would have to keep `prov[].bbox` (and `charspan`) *and* record, for each joined block,
   its offset in the page text it builds with `"\n\n"`, otherwise the retained boxes cannot be addressed
   by a search hit.
6. The exporter — does not exist yet (tickets 09 and 14 are open). Ticket 09 already reserves the shape
   ("page images and boxes named by Source Identity and page number"), which is the right place to
   decide whether the exporter may re-read the corpus PDFs to compute boxes, or whether the flow must
   declare them.

## 8. The fallback, and what the page should say

Four states, four labels — the distinction must be visible in the UI, not just in a footnote:

| State | Page shows | Copy (glossary vocabulary) |
|---|---|---|
| Native text layer, match inside a line | Highlight on the page image **and** in the text, exact | "marked on the page (text layer)" |
| Native page, match crosses a line the cleaner dropped | Highlight on both, marked approximate | "marked on the page, approximate — the match spans a line this page's Page Text does not contain" |
| PDF has no text layer (the 2 scans) | Text-only highlight; no mark on the page | "this page is a scan; its Page Text came from Docling OCR, so the match cannot be marked on the page" — plus, if block boxes are retained later, "approximate: the OCR block that contains the match" |
| OCR text layer inside a PDF (Tesseract) | Highlight on both, marked approximate | "marked on the page, approximate — boxes come from the PDF's OCR text layer" |

The measured basis for those buckets: 100% of within-line windows are exact, 96.4% of across-line windows
are; 0 of 3 scan pages have any geometry; 0 of 310 pages are rotated or cropped differently from their
MediaBox.

Two side effects worth writing down before the copy is fixed: the side-by-side page image + extracted
text *always* reproduces the "OCR text is not the page" story for the scans (the OCR reads `Tine (UIC )`
for `Time (UTC)` and mangles every handwritten field), and a highlight on the *text* half is a
character-offset operation the browser can do with no geometry at all — so the text-only state is free,
and the page-side state is the part that needs the export contract.

## Implications

1. **Ticket 09 can commit to page-side highlighting for the native lane now.** It is exact, it needs one
   formula (`word_box * rotation_matrix * dpi/72`), and 100% of within-line matches are recoverable —
   either by precomputing rects with `search_for` for known queries or by shipping the word box map
   (1.87 MiB) and letting the browser mark tokens by position. Budget the "approximate" state for line-break-straddling
   matches and for the Tesseract-layered page.
2. **The two scans must be declared highlight-less in the demo's first version.** Not "approximate on the
   page": impossible, because the PDF contains no text. Anything else is a false claim about the
   evidence. Retaining Docling's `prov[].bbox` would buy block-approximate boxes for a few KB of JSON, at
   the cost of a declared-output change and a join-offset bookkeeping in the adapter — a later ticket,
   not this one.
3. **The size budget is a dpi and colour decision, not a highlight decision.** 18.5 MiB (greyscale PNG
   @72) or 36.5 MiB (@110) or 56.1 MiB (@144) for all 313 pages; JPEG q60 buys almost nothing over
   greyscale PNG at 72 dpi and loses text crispness. WebP is unavailable without a new dependency. If
   18–36 MiB is too heavy, the lever is *which* pages ship or lazy per-page loading, not the encoding.
4. **Bake a word-level box map at export: 1.87 MiB, 6.3 KiB/page.** Character boxes are 6.6× larger and
   buy nothing a highlighter needs.
5. **Give the exporter a geometry source it is allowed to read.** Either `PdfDocument.page_words()` is
   declared alongside the page text, or ticket 09 must explicitly allow the exporter to re-read the
   corpus PDFs. As the code stands there is no third option: `.demo/out/` contains no coordinates at
   all.

## Open questions

- **Which pages actually ship?** The 313-page total is what the ticket asks for, but the page only ever
  shows a cited page. Shipping images for the ~100 cited pages instead of 313 would cut every number in
  §6 by two-thirds — and the page count is already drifting (325 corpus pages, 313 published by Docling,
  310 published by pymupdf).
- **Is a 36 MiB static bundle acceptable, and is the budget about repo size or first-load size?** Those
  differ by an order of magnitude once per-page lazy loading is on the table; the measurements here are
  bytes on disk, uncompressed, which is the honest upper bound for both.
- **May the demo depend on a new image library (Pillow / `cwebp`) for WebP?** Not decided here, because
  installing was out of bounds; no WebP number here is measured, only the fact that the encoder is absent.
- **Is the OCR text of the scans ever cited?** If the demo's questions avoid the scans (the runbook says
  the agent questions do), then the 3 highlight-less pages may never be opened, and the "impossible"
  case becomes copy rather than engineering.
- **Does the cleaner need to be reversible for highlighting?** The 3.6% cross-line miss rate comes from
  dropping page-number lines and reflowing whitespace. A cleaner that recorded "removed line at offset"
  would let the exporter restore adjacency; nobody has asked for it yet.
- **Rotation and CropBox have never been exercised by this corpus.** The formula is proved, but no page
  here needs it. A future corpus with `/Rotate` would silently break any implementation that skips
  `rotation_matrix`, and nothing in the test suite would notice.

## Appendix: what was run

All measurements are reproducible from this checkout with `.venv/bin/python`, no network, no model:

- **Word and character boxes**: `pymupdf.open(path)[i].get_text("words")` and `get_text("rawdict")` over
  all 18 PDFs; 75,999 words / 505,571 characters on the 310 published pages.
- **Rotation and CropBox**: a synthetic page with one glyph, `set_rotation(0/90/180/270)` and
  `set_cropbox(Rect(100,100,500,700))`, then `get_pixmap(dpi=144)` and `Pixmap.pixel()` ink counts inside
  raw vs `rotation_matrix`-mapped boxes.
- **`search_for` recall**: for every record in `.demo/out/pages/*.jsonl`, 5-token windows taken from
  inside each published line (24,000 windows) and 7-token windows joining adjacent published lines
  (1,050 windows).
- **Render bytes**: every page of every PDF rendered at 72/110/144 dpi as RGB and greyscale, then encoded
  as PNG, JPEG q60 and JPEG q75 (3,900 encodes over 325 pages, 4 threads, 166 s); aggregated per config,
  per page, and for the 313-page bundle set.
- **Coordinate payload**: compact JSON of the word boxes and of the `rawdict` char boxes for the 310
  published pages.
- **Docling schema**: `docling_core/types/doc/common/reference.py`, `…/doc/base.py` and
  `…/document.py` from `docling-project/docling-core` (`main`), read as the published model, not a local
  install.
