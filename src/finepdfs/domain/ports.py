"""The interfaces the outside world must satisfy.

An adapter is a real implementation; a fake is a small class written by hand in a test. Both satisfy
the same interface, which is why the pipeline can be exercised with no network, no GPU and no models.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Protocol

from finepdfs.domain.chunks import Chunk
from finepdfs.domain.crawl import CrawlQuery, CrawlRecord
from finepdfs.domain.demo import UsageSnapshot
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, TextLayerQuality
from finepdfs.domain.language import PageLanguage
from finepdfs.domain.publishing import PageRecord
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
    answers the shared contract, and raises ``SearchUnavailable`` when the index cannot be read at all
    -- an absent, empty or corrupted index is an answer, never a shorter hit list.

    ``record_freshness`` and ``state`` are how the index remembers which declared evidence it mirrors:
    a reader compares that fingerprint with the current declared chunk set before trusting a hit.
    ``state`` raises ``SearchUnavailable`` for the same reason ``search`` does.
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


class GateLedger(Protocol):
    """What the demo's Spend Gates have already cost: the counters behind ``SpendGatePolicy``.

    The ledger owns time, so the domain policy stays pure and reads a snapshot instead of a clock. A read
    enters the client's rate-limit window when it is admitted; OCR pages are charged to the current day
    when the work is admitted, because an admitted read spends the GPU whether or not it finishes. There
    is deliberately no way to give a charge back.
    """

    def usage(self, client: str) -> UsageSnapshot:
        """What ``client`` has read recently and how many OCR pages the demo has charged today."""
        ...

    def record_read(self, client: str) -> None:
        """Count one admitted read against ``client``'s window."""
        ...

    def charge_ocr_pages(self, pages: int) -> None:
        """Charge ``pages`` OCR pages to the current day's budget."""
        ...


class DeclaredPageReader(Protocol):
    """Where one Document's declared record, exclusion and accepted pages are read from.

    Every answer is declared state written by the flow, addressed by the Document's Source Identity: an
    absent file is ``None``, never a guess, and nothing is reconstructed from a projection.
    """

    @property
    def root(self) -> Path: ...

    def has_output(self) -> bool:
        """Whether there is an output directory to read at all.

        This is the adapter's own definition -- ``DiskPageReader`` answers with the directory's
        existence -- and it is deliberately not "a completed run declared something": a Document absent
        from an existing directory is the methods' ``None`` answer, not "no output".
        """
        ...

    def published_digest(self, doc_id: str) -> str | None:
        """The digest of the Document's current published record, or None when it has none."""
        ...

    def exclusion(self, doc_id: str) -> tuple[str, str] | None:
        """The exclusion kind and reason for a Document that was dropped, failed or duplicated."""
        ...

    def accepted_pages(self, doc_id: str) -> Sequence[PageRecord] | None:
        """The accepted Page Texts in original page order, or None when no export is declared."""
        ...
