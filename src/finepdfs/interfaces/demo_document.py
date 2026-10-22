"""The demo's document pane: one job's completed pages, and the shape of the answers that carry it.

ADR-0014 puts the rendered document beside the raw read. The pane is built from Page Text and nothing
else: the pages a Reading exports, kept in their original 1-based numbers, never from a raw delta and
never with a cleanup of the demo's own -- a page carrying marker residue renders that residue, because
the fix for an engine's markers belongs in that engine's adapter and lands in the corpus too.

The store is keyed by job id and bounded like the job answer it belongs to. A page enters it exactly
when it is complete -- from the job's finished Reading, or, while a lane reports as it reads, from the
``ReadSink.on_page`` ``JobPagesSink`` hands the engine. A half-read page is therefore never in the store,
so a half-parsed table can never be rendered: the pane shows a skeleton for a page number until the
completed Page Text for that number exists.

Two answers carry the pane, and the job JSON never does (ADR-0014):

- ``GET /api/read/<id>/document`` -- ``text/html; charset=utf-8``, an inert fragment rendered from the
  completed Page Texts (``interfaces.demo_render``). It is ``<article class="document" data-pages="N"
  data-completed="M">``, and inside it one ``<section class="document-page" data-page="K"
  data-state="complete">`` per completed page in numeric order, each labelled ``page K``, with
  ``<section class="document-page" data-page="K" data-state="pending">`` and a ``.skeleton`` paragraph
  for every page that has not completed. ``data-pages`` is present only when the job knows its page
  count. The answer is ``503`` with ``{"error": ... "rendering is not installed ...", "rendering":
  "not-installed"}`` when the ``[demo]`` extra is absent: the pane never falls back to unescaped text.
- ``GET /api/read/<id>/document.md`` -- ``text/markdown; charset=utf-8``, the completed Page Texts in
  the shared projection (``domain.publishing.document_markdown``), which is the `.md` the page offers
  for download and the same bytes the corpus would write for those pages. It needs no optional
  dependency, so it stays available when rendering does not.

This module is the store alone: no HTTP and no markdown library. ``demo_server`` composes it and
``demo_render`` renders it.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock

from finepdfs.domain.document import PageText

# How many jobs' documents stay answerable. It matches ``demo_jobs.DEFAULT_RETAINED_JOBS``; a finished
# job the queue has forgotten is a 404 before the store is ever asked, so this bound only keeps the two
# from disagreeing about how long "recently finished" lasts (ADR-0012 keeps nothing across a restart).
DEFAULT_RETAINED_DOCUMENTS = 32


@dataclass(frozen=True, slots=True)
class DocumentView:
    """One job's document as the pane sees it now: completed pages, and how many exist in all.

    ``pages`` are the Page Texts that are complete, in their original 1-based numeric order. ``total``
    is the page count the job knows -- from routing or from the finished Reading -- and ``None`` when
    the job has not learned it yet; the pane then shows the next page as a skeleton instead of claiming
    a page count it does not know.
    """

    pages: tuple[PageText, ...] = ()
    total: int | None = None

    @property
    def completed(self) -> int:
        """How many pages are ready to read."""
        return len(self.pages)


@dataclass(slots=True)
class _Entry:
    """The live record one job's pages are collected in, under the store's lock."""

    pages: dict[int, PageText] = field(default_factory=dict)
    total: int | None = None


class DocumentStore:
    """The completed Page Texts of every recent job, one document per job id.

    ``on_page`` is called from the thread doing the Reading and the HTTP handler reads a view from its
    own thread, so every mutation and every view is taken under one lock and a view is an immutable
    snapshot of the pages present at that moment.
    """

    def __init__(self, retained: int = DEFAULT_RETAINED_DOCUMENTS) -> None:
        self._retained = max(retained, 0)
        self._documents: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = Lock()

    def begin(self, job_id: str, page_count: int | None = None) -> None:
        """Start (or keep) a job's document, and record its page count when the job has learned it."""
        with self._lock:
            entry = self._entry(job_id)
            if page_count is not None:
                entry.total = page_count

    def page(self, job_id: str, page: PageText) -> None:
        """Record one completed Page Text; a repeated number replaces the earlier telling."""
        with self._lock:
            self._entry(job_id).pages[page.number] = page

    def completed(self, job_id: str, pages: tuple[PageText, ...] | list[PageText]) -> None:
        """Record the pages a finished Reading answered with, which are authoritative for the job."""
        with self._lock:
            entry = self._entry(job_id)
            entry.pages = {page.number: page for page in pages}
            entry.total = len(entry.pages)

    def view(self, job_id: str) -> DocumentView:
        """A snapshot of the job's document; an id with no document is the empty view, not an error."""
        with self._lock:
            entry = self._documents.get(job_id)
            if entry is None:
                return DocumentView()
            pages = tuple(entry.pages[number] for number in sorted(entry.pages))
            return DocumentView(pages=pages, total=entry.total)

    def sink(self, job_id: str, page_count: int | None = None) -> JobPagesSink:
        """A ``ReadSink`` for one job's Reading: pages land here the moment each one completes."""
        self.begin(job_id, page_count)
        return JobPagesSink(self, job_id)

    def _entry(self, job_id: str) -> _Entry:
        """The job's live entry, created on first use and bounded by the store's retention."""
        entry = self._documents.get(job_id)
        if entry is None:
            entry = _Entry()
            self._documents[job_id] = entry
            while self._retained and len(self._documents) > self._retained:
                self._documents.popitem(last=False)
        return entry


class JobPagesSink:
    """One job's ``ReadSink``: ``on_page`` is kept, ``on_delta`` is not.

    A raw delta is the transcript pane's (ADR-0014's middle pane); the document pane is built from the
    exported Page Text only, so there is deliberately no path from a delta into this store.
    """

    def __init__(self, store: DocumentStore, job_id: str) -> None:
        self._store = store
        self._job_id = job_id

    def on_delta(self, page_number: int, text: str) -> None:
        """Nothing: a delta is not Page Text and the document pane never renders one."""

    def on_page_count(self, page_count: int) -> None:
        """Learn how many pages the read will report, so every page's placeholder exists from the start."""
        self._store.begin(self._job_id, page_count)

    def on_page(self, page: PageText) -> None:
        """Keep a page the moment its read is complete."""
        self._store.page(self._job_id, page)