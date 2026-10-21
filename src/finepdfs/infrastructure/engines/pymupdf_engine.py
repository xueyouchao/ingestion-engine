"""The native text layer, read locally and for free."""

from __future__ import annotations

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
from finepdfs.infrastructure.engines.pdf_document import MuPDFDocument, pymupdf_version


class PymupdfTextLayerProbe:
    """Measures the embedded text of a PDF."""

    def measure(self, content: bytes) -> TextLayerQuality:
        with MuPDFDocument(content) as pdf:
            return pdf.measure_text_layer()


class PymupdfTextEngine:
    """Reads whatever text the PDF already carries."""

    def __init__(self, detail: str | None = None) -> None:
        self._identity = EngineIdentity(name="pymupdf", detail=detail if detail is not None else pymupdf_version())

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document) -> ExtractedDocument:
        with MuPDFDocument(document.content) as pdf:
            pages: tuple[PageText, ...] = pdf.page_texts()
        return ExtractedDocument(pages=pages, engine=self._identity)
