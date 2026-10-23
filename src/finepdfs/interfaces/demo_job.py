"""One Reading's per-job state: its stage, its answer, its raw transcript and its document view.

ADR-0012 makes an upload a job rather than a long request, and ADR-0014 puts two panes beside the input:
the raw transcript the worker emitted and the document the read exported. Both are views of *one* job, so
one object owns all of it and every pane reads it -- the events endpoint asks the job for its transcript,
the document answer for its view, and ``GET /api/read/<id>`` for its eight keys. Nothing is merged after
the fact: a third pane is a method here, not a second store for the server to join.

The job is the ``ReadSink`` the engine reports to and the ``PageCountSink`` the use case reports
routing's page count to: the engine hands it every delta and every completed Page Text, and the use case
hands it the count before the first delta. A delta goes to the transcript and never to the document,
because that pane is built from Page Text only and a half-parsed table is therefore never rendered.

**A finished Reading is authoritative, and it is held once.** Its Page Texts are the accepted ones, so
the pane reads them from the outcome and the job drops whatever a lane reported mid-read rather than
keeping a copy beside the answer -- one holder, which is what "the demo keeps nothing" means for a
finished job (ADR-0012). A failed read keeps the pages that completed before the failure, because they
were really read. **The transcript is ephemeral**: the job frees it the moment the read ends, so the
events endpoint answers ``410`` for a job that finished and no text of a read outlives it (ADR-0014).

Two answers carry the panes, and neither is the job JSON (ADR-0014):

- ``GET /api/read/<id>/document`` -- ``text/html; charset=utf-8``, an inert fragment rendered from the
  completed Page Texts (``interfaces.demo_render``). It is ``<article class="document" data-pages="N"
  data-completed="M">``, and inside it one ``<section class="document-page" data-page="K"
  data-state="complete">`` per completed page in numeric order, each labelled ``page K``, with
  ``<section class="document-page" data-page="K" data-state="pending">`` and a ``.skeleton`` paragraph
  for every page that has not completed, and ``data-state="unread"`` naming the pages a failed read
  never reached. ``data-pages`` is present only when the job knows its page count, and a page the job
  has not seen is only drawn when that count says it exists. The answer is ``503`` with ``{"error": ...
  "rendering is not installed ...", "rendering": "not-installed"}`` when the ``[demo]`` extra is absent:
  the pane never falls back to unescaped text.
- ``GET /api/read/<id>/document.md`` -- ``text/markdown; charset=utf-8``, the completed Page Texts in
  the shared projection (``domain.publishing.document_markdown``), which is the `.md` the page offers
  for download and the same bytes the corpus would write for those pages. It needs no optional
  dependency, so it stays available when rendering does not.

This module is the job alone: no HTTP and no markdown library. ``demo_jobs`` owns the queue that admits
and retires jobs, ``demo_render`` renders a view, and ``demo_server`` composes the three.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

from finepdfs.application.demo_read import FailedReading, Reading, ReadingOutcome, Refusal
from finepdfs.domain.demo import ReadingStage
from finepdfs.domain.document import PageText
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.domain.publishing import pages_in_order
from finepdfs.interfaces.demo_transcript import RawTranscript


@dataclass(frozen=True, slots=True)
class DocumentView:
    """One job's document as the pane sees it now: completed pages, and how many exist in all.

    ``pages`` are the Page Texts that are complete, in their original 1-based numeric order. ``total``
    is the page count the job knows -- from the count the use case reported before the read started, or
    from the finished Reading -- and ``None`` when the job has not learned it yet; the pane then shows
    only the pages the read reported instead of claiming a page count it does not know. ``failed`` is
    whether the read failed: the pages it never reached were not read, and the pane says so rather than
    promising a placeholder that no later page will replace.
    """

    pages: tuple[PageText, ...] = ()
    total: int | None = None
    failed: bool = False

    @property
    def completed(self) -> int:
        """How many pages are ready to read."""
        return len(self.pages)


class DemoJob:
    """One upload's per-job state, as every pane sees it.

    ``advance`` is the stage the read reached and ``finish`` is what it answered; ``on_delta`` and
    ``on_page`` are what the engine reports (``domain.ports.ReadSink``), and ``on_page_count`` is what the
    use case reports before the engine runs at all (``domain.ports.PageCountSink``). The reader thread
    writes and a handler thread reads, so every field is touched under one lock and every view is a copy
    taken under it. ``redact`` is what a failure's words are shown through on the wire: a configured address or
    token in an engine's error is a leak, and the composition root is the one place that knows both.
    """

    def __init__(
        self,
        job_id: str,
        name: str,
        *,
        transcript_bytes: int,
        redact: Callable[[str], str] = str,
    ) -> None:
        self.id = job_id
        self.name = name
        self._redact = redact
        self._lock = Lock()
        self._stage: ReadingStage = "queued"
        self._outcome: ReadingOutcome | None = None
        self._broken: FailedReading | None = None
        self._pages: dict[int, PageText] = {}
        self._total: int | None = None
        # Zero means the operator switched the transcript off: no store, and the events endpoint says so.
        self._transcript = RawTranscript(transcript_bytes) if transcript_bytes > 0 else None

    # -- what the panes ask -------------------------------------------------------------------------

    @property
    def stage(self) -> ReadingStage:
        """The stage the read is at; ``done`` once the job published its answer."""
        with self._lock:
            return self._stage

    def transcript(self) -> RawTranscript | None:
        """The raw transcript while the read runs; ``None`` once it finished or was switched off."""
        with self._lock:
            return self._transcript

    def document(self) -> DocumentView:
        """The completed pages in page order, the page count the job knows, and whether it failed."""
        with self._lock:
            if isinstance(self._outcome, Reading):
                pages = pages_in_order(self._outcome.pages)
                return DocumentView(pages=pages, total=len(pages))
            return DocumentView(
                pages=pages_in_order(self._pages.values()),
                total=self._total,
                failed=isinstance(self._outcome, FailedReading),
            )

    def answer(self) -> dict[str, Any]:
        """The job exactly as ``GET /api/read/<id>`` answers it: one shape, with nulls where it is absent."""
        with self._lock:
            reading = self._outcome if isinstance(self._outcome, Reading) else None
            failure = _failure(self._outcome) or (self._broken.payload() if self._broken is not None else None)
            return {
                "job": self.id,
                "stage": self._stage,
                "outcome": _outcome(self._outcome),
                "elapsed_ms": reading.elapsed_ms if reading is not None else None,
                "warmed_ms": reading.warm_ms if reading is not None else None,
                "refusal": _refusal(self._outcome),
                "failure": failure,
                "reading": _reading(reading),
            }

    # -- lifecycle, called by the Reading Queue -----------------------------------------------------

    def advance(self, stage: ReadingStage) -> None:
        """The stage the read has reached.

        ``done`` is not taken from here: the job publishes its end under the lock together with the
        answer it ended with, so a poll in between never sees a job that says it finished and has no
        answer.
        """
        if stage == "done":
            return
        with self._lock:
            self._stage = stage

    def finish(self, outcome: ReadingOutcome | None, broken: FailedReading | None = None) -> None:
        """Publish the answer, end the transcript and free it: the job retires itself.

        ``broken`` is the queue's own record for a reader that raised before the use case could turn it
        into one of its three outcomes. It is already shown through the configured redaction where it
        was built (``FailedReading.broke``), so the transcript and the job answer carry one telling of
        it; the stream is shown through ``redact`` once more, which is idempotent for that reason.
        """
        with self._lock:
            self._outcome = outcome
            self._broken = broken
            self._stage = "done"
            if isinstance(outcome, Reading):
                # The Reading's accepted pages are the document now, and they are held once: the pages a
                # lane reported mid-read are dropped rather than kept beside them.
                self._pages.clear()
                self._total = None
            transcript = self._transcript
            self._transcript = None
        if transcript is None:
            return
        failure = broken if broken is not None else _failed(outcome)
        if failure is not None:
            payload = failure.payload()
            transcript.fail(payload["kind"], payload["lane"], self._redact(payload["reason"]))
        transcript.finish("failed" if broken is not None else _outcome(outcome))

    # -- ReadSink, called from the engine-call pool threads ------------------------------------------

    def on_delta(self, page_number: int, text: str) -> None:
        """One raw delta, kept for the transcript pane; the document pane never renders a delta."""
        transcript = self._transcript
        if transcript is not None:
            transcript.on_delta(page_number, text)

    def on_page(self, page: PageText) -> None:
        """One completed Page Text: the document keeps it, and the stream reports it to the raw transcript.

        The page event is what a visitor watching the middle pane sees when a page completes, so it is
        reported before the page is kept: the two panes are fed from one report, not two.
        """
        transcript = self._transcript
        if transcript is not None:
            transcript.on_page(page)
        with self._lock:
            self._pages[page.number] = page

    def on_page_count(self, page_count: int) -> None:
        """Learn how many pages the read will report, before its first delta: routing's answer, not a lane's."""
        with self._lock:
            self._total = page_count


def _outcome(outcome: ReadingOutcome | None) -> str | None:
    if isinstance(outcome, Reading):
        return "read"
    if isinstance(outcome, Refusal):
        return "refused"
    if isinstance(outcome, FailedReading):
        return "failed"
    return None


def _failed(outcome: ReadingOutcome | None) -> FailedReading | None:
    return outcome if isinstance(outcome, FailedReading) else None


def _refusal(outcome: ReadingOutcome | None) -> dict[str, str] | None:
    if isinstance(outcome, Refusal):
        return {"kind": outcome.kind, "reason": outcome.reason}
    return None


def _failure(outcome: ReadingOutcome | None) -> dict[str, str] | None:
    failure = _failed(outcome)
    return None if failure is None else failure.payload()


def _reading(reading: Reading | None) -> dict[str, Any] | None:
    """The Reading as the page shows it: the decision, its evidence, the engine and the pages.

    The pages keep the numbers the lane read them at -- original, 1-based -- so a citation still means
    the page the visitor is looking at, whatever order the engine returned them in.
    """
    if reading is None:
        return None
    pages = pages_in_order(reading.pages)
    return {
        "lane": reading.plan.lane,
        "reason": reading.plan.decision.reason,
        "features": dict(reading.plan.decision.features),
        "thresholds": _thresholds(reading.thresholds),
        "engine": str(reading.engine),
        "page_count": len(pages),
        "character_count": sum(len(page.text) for page in pages),
        "pages": [{"number": page.number, "text": page.text} for page in pages],
    }


def _thresholds(policy: OcrRoutingPolicy) -> dict[str, float]:
    """The thresholds the features were measured against, exactly as the policy holds them."""
    return {
        "min_characters_per_page": policy.min_characters_per_page,
        "min_text_coverage": policy.min_text_coverage,
        "max_garbled_ratio": policy.max_garbled_ratio,
        "max_image_dominated_share": policy.max_image_dominated_share,
    }
