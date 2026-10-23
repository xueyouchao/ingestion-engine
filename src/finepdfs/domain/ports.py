"""The interfaces the outside world must satisfy.

An adapter is a real implementation; a fake is a small class written by hand in a test. Both satisfy
the same interface, which is why the pipeline can be exercised with no network, no GPU and no models.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, runtime_checkable

from finepdfs.domain.chunks import Chunk
from finepdfs.domain.crawl import CrawlQuery, CrawlRecord
from finepdfs.domain.demo import UsageSnapshot
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
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


@runtime_checkable
class ReadSink(Protocol):
    """A caller watching an Extraction happen, page by page (ADR-0014).

    An engine that streams reports to it. ``on_delta`` is the engine's own output as it arrives, exactly
    as the worker emitted it and before any normalization -- echoed prompt, fences and layout markers
    included -- tagged with the 1-based page number it belongs to, because a read may have several pages
    in flight at once. Those calls come from whichever engine threads are reading those pages, so a sink
    may be called by several threads at the same time and has to be safe to. It may also be called after
    the read has failed: a request a worker has already started cannot be cancelled, so deltas for a
    sibling page keep arriving until that page's stream ends, and a sink that has given up must ignore
    them rather than treat them as a second answer (ADR-0010, ADR-0014). ``on_page`` is called on the
    thread that started the read, in page-number order, and carries the Page Text the read exports for a
    page as soon as that page is complete: the same text a batch run would export for the same page.

    An engine with nothing arriving page by page -- the text layer, a local command that writes its
    results when it finishes -- has nothing to report and calls neither of those, so a caller that
    received nothing knows the lane did not stream instead of inferring a cadence of its own.

    These two are everything an engine reports, so they are the whole port. The page count is not an
    engine capability -- routing answers it before any lane runs -- and a watcher that wants it is also
    a ``PageCountSink``, which the use case asks separately.
    """

    def on_delta(self, page_number: int, text: str) -> None: ...

    def on_page(self, page: PageText) -> None: ...


@runtime_checkable
class PageCountSink(Protocol):
    """A watcher that wants the page count routing reported, before the first delta (ADR-0014).

    No engine calls this. The use case that starts the read offers routing's answer once, before a lane
    runs, so a viewer can draw a placeholder for every page from the start instead of inventing a count
    from the pages that arrived. It is a protocol of its own because the count is not an engine
    capability and the engine's sink may only promise what an engine can report; a watcher with no use
    for the count simply does not implement it, and is never asked.
    """

    def on_page_count(self, pages: int) -> None: ...


class OcrEngine(Protocol):
    """Turns document bytes into page texts."""

    @property
    def identity(self) -> EngineIdentity: ...

    def extract(self, document: Document, sink: ReadSink | None = None) -> ExtractedDocument:
        """Read the document; a caller may pass a sink to watch the read and may always leave it out.

        The answer is the same either way -- the sink only reports what the read is doing as it happens
        -- so a caller with nothing to watch, and every batch run, passes no sink and is served exactly
        as before.
        """
        ...


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


class OcrEndpointUnavailable(Exception):
    """Raised by an endpoint probe that cannot ask the endpoint at all, and the fault that stopped it.

    A worker that is merely cold is not this: that is "not ready yet", the demo's warming stage. This is
    the endpoint that cannot come -- no address configured, an address that is not one, or a bearer the
    endpoint refuses -- so no amount of waiting helps and the caller fails the read instead of waiting.
    The message names the fault and never the credentials or the address, both of which are a leak, not
    a diagnosis: a token in a log or a page is a bill.
    """


class OcrEndpointProbe(Protocol):
    """Whether the demo's OCR endpoint answers right now: the question behind a cold worker's warming.

    ``ready`` answers ``False`` for anything that may resolve with time -- a connection error, a timeout,
    a 5xx while the worker is still loading, a 429 asking for less -- so the caller can poll, and raises
    ``OcrEndpointUnavailable`` for a fault that waiting cannot fix: no address configured, an address that
    is not one, a bearer the endpoint refuses, or a route it does not serve.
    """

    def ready(self) -> bool: ...


class OcrWorker(Protocol):
    """The OCR lane's worker for one Reading, handed out as a context manager and given back after it.

    ``worker()`` answers a *fresh* context manager, because a worker belongs to one Reading: entering it
    is what rents one -- starting a container, publishing the address and bearer the engine and the probe
    are built from -- and leaving it is what gives it back. The use case enters it only after the Spend
    Gates have let an OCR read through, so a native upload or a refusal never rents anything, and leaves
    it before the Reading answers, so the wait it took to come up is part of the reported number.

    Entering raises when the worker cannot be had at all -- a provider whose start command fails, a lease
    it refuses -- and the caller answers that as a refusal of kind ``unavailable``, because the gates did
    not close and no lane was chosen.
    """

    def __call__(self) -> AbstractContextManager[tuple[OcrEngine, OcrEndpointProbe]]: ...


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
