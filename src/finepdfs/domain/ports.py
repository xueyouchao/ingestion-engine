"""The interfaces the outside world must satisfy.

An adapter is a real implementation; a fake is a small class written by hand in a test. Both satisfy
the same interface, which is why the pipeline can be exercised with no network, no GPU and no models.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from finepdfs.domain.crawl import CrawlQuery, CrawlRecord
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, TextLayerQuality
from finepdfs.domain.language import PageLanguage


class CrawlSource(Protocol):
    """Where Crawl Records come from, and where their bytes come from."""

    def list_records(self, query: CrawlQuery) -> Sequence[CrawlRecord]: ...

    def fetch(self, record: CrawlRecord) -> bytes:
        """The PDF payload of one record."""
        ...


class TextLayerProbe(Protocol):
    """Measures what the embedded text of a PDF looks like."""

    def measure(self, content: bytes) -> TextLayerQuality: ...


class OcrEngine(Protocol):
    """Turns document bytes into page texts."""

    @property
    def identity(self) -> EngineIdentity: ...

    def extract(self, document: Document) -> ExtractedDocument: ...


class LanguageIdentifier(Protocol):
    """Names the language or script of one page's text.

    The tag is in the vocabulary of whichever adapter is wired -- `FINEPDFS_LANGUAGE` selects it -- so a
    caller treats it as an opaque string and compares it against an allow-list in that same vocabulary.
    """

    def identify(self, text: str, page_number: int) -> PageLanguage: ...
