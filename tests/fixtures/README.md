# Test fixtures

Offline inputs for the default suite. The real Corpus lives in
[`src/finepdfs/data/pdf-demo-corpus-manifest.json`](../../src/finepdfs/data/pdf-demo-corpus-manifest.json)
(package data, replayed by `finepdfs-demo restore`) and is restored from Common Crawl; the files here
are not crawled PDFs unless this file says so.

## Controlled fixtures added for Corpus coverage

These are **constructed, not crawled**. They exercise the failed-Document and page-language paths the
pinned Corpus cannot currently demonstrate. They are not evidence of extraction quality on real PDFs
and must never be presented as such.

- **`not_a_pdf.pdf`** (258 bytes, plain text inside) — a `%PDF-1.7` header followed by prose, with no
  body and no xref table. PyMuPDF rejects it, so the flow records the Document as a failed Exclusion
  while its neighbours complete (`tests/infrastructure/test_flow_end_to_end.py`). Written on
  2026-10-18; its content is readable prose, so its provenance needs no reconstruction.
- **`mixed_language.pdf`** (~39 KB) — three pages: English, Spanish (both Latin script) and Russian
  (Cyrillic). It shows that the default `script` Language Mode sees one `Latn` tag across English and
  Spanish and drops the Cyrillic page as the minority script. It does not show a mode that names the
  two Latin-script languages separately; exercising that needs a Language Mode whose model is present
  (see `docs/GLOSSARY.md` and `FINEPDFS_LANGUAGE_MODEL`).
  Generated on 2026-10-18 with PyMuPDF 1.28.2 and DejaVu Sans, subset so the fixture stays small. The
  generation shape was:

  ```python
  import pymupdf

  pages = [english_text, spanish_text, russian_text]
  doc = pymupdf.open()
  for text in pages:
      page = doc.new_page()
      page.insert_textbox(
          pymupdf.Rect(72, 72, 540, 720), text, fontsize=12,
          fontname="dejavu", fontfile="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
      )
  doc.set_metadata({"title": "controlled mixed-language fixture (English, Spanish, Russian)"})
  doc.subset_fonts()
  doc.save("tests/fixtures/mixed_language.pdf", garbage=4, deflate=True)
  ```

The other files in this directory predate the Corpus coverage work and are unchanged.

## Scanned image fixtures

A scan is a picture of a page, so it carries no text layer at all — MuPDF reads an image as one page
per frame, and the demo routes it to the OCR lane. These are **constructed, not photographed**: white
pages with three lines of text drawn in Pillow's default font, written by the system Pillow
(`/usr/bin/python3`, Pillow 10.2.0) on 2026-10-19 with:

```python
from PIL import Image, ImageDraw

frames = []
for number in (1, 2, 3):
    image = Image.new("L", (480, 640), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 60), f"Scanned page {number}.", fill="black")
    draw.text((40, 100), "This page carries no text layer: only a picture of", fill="black")
    draw.text((40, 120), "words, so it has to be sent to the OCR worker.", fill="black")
    frames.append(image)
frames[0].save("tests/fixtures/scan.png")
frames[0].save("tests/fixtures/scan.jpg", quality=75)
frames[0].save("tests/fixtures/scan-3-pages.tiff", compression="tiff_lzw", save_all=True, append_images=frames[1:])
```

- **`scan.png`** (~3.6 KB) and **`scan.jpg`** (~7.2 KB) — one frame each: one page, no text, routed to OCR.
- **`scan-3-pages.tiff`** (~13 KB) — three LZW frames, read as three pages, so the page cap and the OCR
  budget count three (`tests/infrastructure/test_pdf_adapters.py`, `tests/interfaces/test_demo_server.py`).

Their text is legible to a model, which is deliberate — a real cold-start rehearsal sends one — but the
default suite never runs an engine on them.

## Streaming and rendered-document fixtures (ADR-0014)

Both are **constructed, not captured**. They exist to pin one contract each, and neither is evidence of
what a deployed worker or a corpus run produced.

- **`paddleocr_stream_page.sse`** — one PaddleOCR-VL page as a vLLM-style SSE body: `data:` frames
  shaped like `chat.completion.chunk`s, a role frame, a `finish_reason: "stop"` frame, a usage frame and
  `data: [DONE]`, carrying 168 characters of content in all. The echoed `OCR:` prompt and the
  opening markdown fence arrive split across frames (`"OCR"`, `":"`, `"\n``"`, …), so only
  concatenate-then-normalize-once recovers the page; the Page Text it exports is
  `# Quarterly Report` / `Receipts were 12% higher …` / a two-column `Region | Receipts` table. It was
  hand-written on 2026-10-22 in the shape vLLM sends for this model family and replayed by
  `tests/fake_streaming_worker.py` (`tests/infrastructure/test_vlm_streaming.py`). It is **not** a
  capture from a deployed worker, and no test in the suite talks to one.
- **`pages/ba02d52504f527bb.jsonl`** — three page records for one document (`ba02d52504f527bb`, engine
  `paddleocr-vl@weights-c5630abae1d9`, source URL on `example.org`), each exactly the fields
  `PageRecord.as_json()` writes (`doc_id`, `source_url`, `digest`, `page_number`, `text`, `engine`), so
  `PageRecord(**row)` is the shape assertion. The text is constructed as well. Its job is to pin the
  demo's rendered-document answer as a golden one (`tests/interfaces/test_demo_document_server.py`):
  the pane carries these Page Texts in numeric order behind their `Page N` separators, and its `.md` is
  the same bytes `document_markdown` gives, which is what `out/pages/<key>.jsonl` and
  `out/docs/<key>.md` would hold for those pages. It proves the projection, not what any engine read.
