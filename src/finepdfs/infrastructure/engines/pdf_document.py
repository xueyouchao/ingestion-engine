"""Reading PDFs with PyMuPDF: text, measurements and page images."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

import pymupdf

from finepdfs.domain.document import PageText, TextLayerQuality
from finepdfs.domain.text import garbled_ratio

IMAGE_DOMINATED_CHARACTER_LIMIT = 50


class PdfReadError(RuntimeError):
    """Raised when bytes cannot be opened as a PDF."""


@dataclass
class PdfDocument:
    """One open PDF. Use it as a context manager so the file handle is always released."""

    content: bytes
    _document: pymupdf.Document | None = None

    def __enter__(self) -> Self:
        try:
            self._document = pymupdf.open(stream=self.content, filetype="pdf")
        except Exception as error:
            raise PdfReadError(f"these bytes are not a readable PDF: {error}") from error
        if self._document.page_count == 0:
            raise PdfReadError("this PDF has no pages")
        return self

    def __exit__(self, *_) -> None:
        if self._document is not None:
            self._document.close()
            self._document = None

    @property
    def page_count(self) -> int:
        return self._require_document().page_count

    def page_texts(self) -> tuple[PageText, ...]:
        document = self._require_document()
        return tuple(
            PageText(number=index + 1, text=document.load_page(index).get_text("text"))
            for index in range(document.page_count)
        )

    def render_page_png(self, page_number: int, dpi: int = 200) -> bytes:
        page = self._require_document().load_page(page_number - 1)
        return page.get_pixmap(dpi=dpi).tobytes("png")

    def measure_text_layer(self) -> TextLayerQuality:
        document = self._require_document()
        texts: list[str] = []
        pages_with_text = 0
        image_dominated = 0
        for index in range(document.page_count):
            page = document.load_page(index)
            text = page.get_text("text")
            texts.append(text)
            if text.strip():
                pages_with_text += 1
            elif page.get_images(full=True):
                image_dominated += 1
        joined = "\n".join(texts)
        return TextLayerQuality(
            page_count=document.page_count,
            pages_with_text=pages_with_text,
            character_count=len(joined),
            garbled_ratio=garbled_ratio(joined),
            image_dominated_pages=image_dominated,
        )

    def _require_document(self) -> pymupdf.Document:
        if self._document is None:
            raise PdfReadError("this PDF document is not open")
        return self._document


def pymupdf_version() -> str:
    return pymupdf.version[0]
