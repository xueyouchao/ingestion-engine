# Test fixtures

Offline inputs for the default suite. The real Corpus lives in
[`docs/demo/pdf-demo-corpus-manifest.json`](../../docs/demo/pdf-demo-corpus-manifest.json) and is
restored from Common Crawl; the files here are not crawled PDFs unless this file says so.

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
