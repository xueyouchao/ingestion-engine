"""The interfaces the outside world must satisfy.

An adapter is a real implementation; a fake is a small class written by hand in a test. Both satisfy
the same interface, which is why the pipeline can be exercised with no network, no GPU and no models.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol

from finepdfs.domain.chunks import Chunk
from finepdfs.domain.crawl import CrawlQuery, CrawlRecord
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, TextLayerQuality
from finepdfs.domain.language import PageLanguage
from finepdfs.domain.search import IndexFailure, IndexState, SearchHit, SearchQuery


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

    The tag is in the vocabulary of the adapter that produced it -- a language code for one adapter, a
    script code for another -- so a caller treats it as an opaque string and compares it against an
    allow-list in that same vocabulary.
    """

    def identify(self, text: str, page_number: int) -> PageLanguage: ...


class SearchIndex(Protocol):
    """A lexical index over declared chunks. It never decides what is published or accepted.

    ``upsert`` is an idempotent write of the chunks it is given and reports every chunk it could not
    store; ``retain`` deletes every stored chunk whose id is not in the current declared set, which is
    how replacements, fewer pages, exclusions and removed sources stop being searchable. ``search``
    answers the shared contract.

    ``record_freshness`` and ``state`` are how the index remembers which declared evidence it mirrors:
    a reader compares that fingerprint with the current declared chunk set before trusting a hit.
    ``state`` raises ``SearchUnavailable`` when the index cannot be read at all.
    """

    def upsert(self, chunks: Sequence[Chunk]) -> Sequence[IndexFailure]: ...

    def retain(self, chunk_ids: Collection[str]) -> int:
        """Delete the chunks that are no longer declared; return how many were removed."""
        ...

    def search(self, query: SearchQuery) -> Sequence[SearchHit]: ...

    def record_freshness(self, fingerprint: str) -> None:
        """Remember the fingerprint of the declared evidence this index now mirrors."""
        ...

    def state(self) -> IndexState:
        """The fingerprint this index was built from and how many chunks it holds."""
        ...
