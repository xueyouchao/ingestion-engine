"""One job's per-job state: the stage it reached, what it answered, its transcript and its document.

Every pane reads this one object -- the events endpoint asks it for the transcript, the document answer
for its view, and ``GET /api/read/<id>`` for its eight keys -- so this file is where the pane facts are
pinned without a server: a page's number and text, a page count, an order, a redacted reason. Nothing
here touches HTTP, and nothing here reads a store beside the job, because there is none.
"""

from __future__ import annotations

from finepdfs.application.curate import RoutingPlan
from finepdfs.application.demo_read import FailedReading, Reading, Refusal
from finepdfs.domain.document import EngineIdentity, PageText, TextLayerQuality
from finepdfs.domain.ocr import OcrRoutingPolicy, RoutingDecision
from finepdfs.domain.ports import PageCountSink, ReadSink
from finepdfs.interfaces.demo_job import DemoJob, DocumentView
from finepdfs.interfaces.demo_transcript import RawTranscript

HEALTHY = TextLayerQuality(
    page_count=2, pages_with_text=2, character_count=4000, garbled_ratio=0.0, image_dominated_pages=0
)
PLAN = RoutingPlan(
    quality=HEALTHY,
    decision=RoutingDecision(needs_ocr=False, reason="the text layer is healthy", features={"page_count": 2.0}),
    engine_name="pymupdf",
)
WORKER = "http://127.0.0.1:9/v1"
LEASE_BEARER = "eyJhbGciOiJIUzI1NiJ9.a-lease-token"


def _reading(pages: tuple[PageText, ...] = (PageText(1, "one"), PageText(2, "two"))) -> Reading:
    return Reading(
        plan=PLAN,
        thresholds=OcrRoutingPolicy(),
        engine=EngineIdentity(name="pymupdf", detail="1.28.2"),
        pages=pages,
        elapsed_ms=12,
        warm_ms=None,
    )


def _redact(message: str) -> str:
    return message.replace(WORKER, "<the OCR worker>").replace(LEASE_BEARER, "<the bearer>")


def _job(transcript_bytes: int = 1024) -> DemoJob:
    return DemoJob("job-1", "mine.pdf", transcript_bytes=transcript_bytes, redact=_redact)


def _events(transcript: RawTranscript) -> list[tuple[str, dict]]:
    """The transcript's own events, read whether or not the job still holds it."""
    return [(event.name, event.data) for event in transcript.since(0)[0]]


def test_a_job_is_the_engine_s_sink_and_also_the_one_that_wants_the_page_count() -> None:
    """One watcher answers both questions, but they are two ports: the count is nobody's engine report."""
    job = _job()

    assert isinstance(job, ReadSink), "a lane reports its deltas and pages to the job"
    assert isinstance(job, PageCountSink), "the use case offers the job routing's count before the lane runs"


def test_a_job_that_has_reported_nothing_is_an_empty_document_not_a_guess() -> None:
    job = _job()

    assert job.document() == DocumentView()
    assert job.document().pages == ()
    assert job.document().total is None


def test_a_page_a_lane_reports_lands_in_the_document_and_routings_count_draws_the_rest() -> None:
    job = _job()

    job.on_page_count(3)
    assert (job.document().pages, job.document().total) == ((), 3), "the count is known before any page"

    job.on_page(PageText(1, "the completed first page"))
    view = job.document()

    assert [(page.number, page.text) for page in view.pages] == [(1, "the completed first page")]
    assert view.total == 3, "the pane knows how many pages are yet to come"
    assert view.completed == 1


def test_the_document_is_in_page_number_order_however_the_pages_arrived() -> None:
    job = _job()
    job.on_page_count(3)

    for page in (PageText(3, "third"), PageText(1, "first"), PageText(2, "second")):
        job.on_page(page)

    view = job.document()
    assert [page.number for page in view.pages] == [1, 2, 3]
    assert [page.text for page in view.pages] == ["first", "second", "third"]
    assert view.total == 3


def test_a_delta_is_a_transcript_fact_and_never_document_text() -> None:
    """``on_delta`` is the raw pane's; the document pane is built from exported Page Text only."""
    job = _job()
    transcript = job.transcript()
    assert transcript is not None

    job.on_delta(1, "| a half | parsed table |")

    assert job.document().pages == (), "a delta is never page text"
    assert _events(transcript) == [("delta", {"page": 1, "text": "| a half | parsed table |"})]


def test_a_completed_page_is_reported_on_the_stream_as_well_as_kept_for_the_document() -> None:
    """One report, two panes: the ``page`` event is what the raw pane shows when a page completes."""
    job = _job()
    transcript = job.transcript()
    assert transcript is not None

    job.on_page(PageText(1, "the page"))

    assert _events(transcript) == [("page", {"number": 1, "text": "the page"})]
    assert [(page.number, page.text) for page in job.document().pages] == [(1, "the page")]


def test_a_page_number_that_repeats_is_replaced_rather_than_duplicated() -> None:
    job = _job()

    job.on_page(PageText(1, "a first telling"))
    job.on_page(PageText(1, "the final telling"))

    assert [(page.number, page.text) for page in job.document().pages] == [(1, "the final telling")]


def test_a_breakage_after_the_count_is_a_failed_read_not_a_job_still_reading() -> None:
    """The queue's own record ends the job, so no page it never reached may still be drawn as pending."""
    job = _job()
    job.on_page_count(3)
    job.on_page(PageText(1, "the page that completed before the queue broke"))

    job.finish(None, FailedReading.broke(RuntimeError("the worker could not be built")))

    view = job.document()
    assert view.failed is True, "the queue's breakage is a failure, not a document nothing will complete"
    assert view.total == 3, "the pages the failure never reached keep the numbers routing gave them"
    assert [(page.number, page.text) for page in view.pages] == [
        (1, "the page that completed before the queue broke")
    ], "the page that really completed is kept"


def test_a_refusal_after_the_count_promises_no_page_that_can_never_arrive() -> None:
    """A refusal read nothing, so a count routing gave must not leave a page still being read."""
    refusal = Refusal(
        kind="unavailable",
        reason="the demo could not reach its OCR worker: no address is configured",
        plan=PLAN,
        thresholds=OcrRoutingPolicy(),
    )
    job = _job()
    job.on_page_count(2)

    job.finish(refusal)

    view = job.document()
    assert view.total is None, "no page can ever arrive for a refusal, so no count may promise one"
    assert view.failed is False, "a refusal is not a failure; the refusal panel is the whole answer"
    assert view.pages == ()


def test_a_finished_reading_is_the_document_and_its_page_texts_are_held_once() -> None:
    """ADR-0012 keeps nothing: the accepted pages are the job's view, not a second copy beside it."""
    reading = _reading((PageText(2, "second"), PageText(1, "first")))
    job = _job()
    job.on_page_count(2)
    job.on_page(PageText(1, "a telling from mid-read"))

    job.finish(reading)

    view = job.document()
    assert view.pages == (reading.pages[1], reading.pages[0]), "page order, and the Reading's own objects"
    assert view.total == 2
    assert view.completed == 2


def test_a_failed_read_keeps_the_pages_that_completed_before_it() -> None:
    failure = FailedReading(
        kind="failed",
        lane="ocr",
        reason="the worker broke its stream off",
        plan=PLAN,
        thresholds=OcrRoutingPolicy(),
    )
    job = _job()
    job.on_page_count(2)
    job.on_page(PageText(1, "the page that completed before the failure"))

    job.finish(failure)

    view = job.document()
    assert [(page.number, page.text) for page in view.pages] == [
        (1, "the page that completed before the failure")
    ]
    assert view.total == 2, "the page count routing gave is still the pane's"
    assert job.stage == "done"
    assert job.answer()["failure"] == {"kind": "failed", "lane": "ocr", "reason": failure.reason}


def test_a_refusal_shows_no_page_text_because_none_was_read() -> None:
    refusal = Refusal(
        kind="page-cap",
        reason="this document has 30 pages; the demo reads up to 25",
        plan=PLAN,
        thresholds=OcrRoutingPolicy(),
    )
    job = _job()

    job.finish(refusal)

    assert job.document() == DocumentView()
    assert job.answer()["outcome"] == "refused"
    assert job.answer()["refusal"] == {"kind": "page-cap", "reason": refusal.reason}


def test_a_finished_job_frees_its_transcript_and_ends_the_stream_it_was_serving() -> None:
    job = _job()
    job.on_delta(1, "OCR")
    transcript = job.transcript()
    assert transcript is not None, "a running job keeps its transcript"

    job.finish(_reading())

    assert job.transcript() is None, "the job freed its transcript the moment it finished"
    assert _events(transcript) == [("delta", {"page": 1, "text": "OCR"}), ("done", {"outcome": "read"})]


def test_a_transcript_ceiling_of_zero_keeps_no_text_at_all() -> None:
    job = _job(transcript_bytes=0)

    job.on_delta(1, "OCR")

    assert job.transcript() is None, "a zero ceiling keeps no transcript at all"
    job.finish(_reading())
    assert job.transcript() is None


def test_the_job_is_not_finished_by_the_stage_a_reader_announces_after_its_last_page() -> None:
    """``done`` is published with the answer, under the job's lock: never on the read's say-so."""
    job = _job()

    job.advance("routing")
    job.advance("reading")
    job.advance("done")

    assert job.stage == "reading"
    assert job.answer()["stage"] == "reading"
    assert job.answer()["outcome"] is None


def test_the_jobs_own_breakage_is_redacted_where_a_visitor_can_read_it() -> None:
    """A breakage reaches the stream and the job answer through the redaction, in one telling.

    Both are public, and a composition that could not build its worker may quote the address and the
    bearer it was handed: ``broke`` shows its words through the redactor that call site was given.
    """
    job = _job()
    job.advance("reading")
    transcript = job.transcript()
    assert transcript is not None
    broken = FailedReading.broke(
        RuntimeError(f"connect to {WORKER} failed with bearer {LEASE_BEARER}"), _redact
    )

    job.finish(None, broken)

    shown = "connect to <the OCR worker> failed with bearer <the bearer>"
    assert _events(transcript) == [
        ("error", {"kind": "failed", "lane": "", "reason": shown}),
        ("done", {"outcome": "failed"}),
    ], "the worker's address and the bearer never reach the stream"
    assert job.transcript() is None
    assert job.answer()["failure"] == {"kind": "failed", "lane": "", "reason": shown}
    assert job.answer()["outcome"] is None, "a breakage is carried in ``failure``, as the demo always answered"
    assert job.stage == "done"
