"""The Reading Queue without HTTP: one Reading at a time, a bounded wait, and the job the page polls.

The queue is the demo's own, not Modal's (ADR-0012), so it is tested on its own: a fake reader stands
where the composition root would build a ``DemoRead`` inside a lease, and the assertions are what a
visitor's browser sees -- a stage, an outcome, a refusal kind, a failure lane -- never a private counter.
Each job is the ``DemoJob`` the queue hands out, so this file also pins that the queue's one retention
bound is the only one: a forgotten job takes its document with it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

import pytest

from finepdfs.application.curate import RoutingPlan
from finepdfs.application.demo_read import FailedReading, Reading, Refusal
from finepdfs.domain.demo import DemoLimits
from finepdfs.domain.document import EngineIdentity, PageText, TextLayerQuality
from finepdfs.domain.ocr import OcrRoutingPolicy, RoutingDecision
from finepdfs.interfaces.demo_jobs import QueueFull, ReadingQueue

HEALTHY = TextLayerQuality(
    page_count=2, pages_with_text=2, character_count=4000, garbled_ratio=0.0, image_dominated_pages=0
)
FEATURES = {
    "page_count": 2.0,
    "characters_per_page": 2000.0,
    "text_coverage": 1.0,
    "garbled_ratio": 0.0,
    "image_dominated_share": 0.0,
}
THRESHOLDS = {
    "min_characters_per_page": 200.0,
    "min_text_coverage": 0.5,
    "max_garbled_ratio": 0.05,
    "max_image_dominated_share": 0.5,
}
PLAN = RoutingPlan(
    quality=HEALTHY,
    decision=RoutingDecision(needs_ocr=False, reason="the text layer is healthy", features=dict(FEATURES)),
    engine_name="pymupdf",
)


def _reading(warm_ms: int | None = None) -> Reading:
    return Reading(
        plan=PLAN,
        thresholds=OcrRoutingPolicy(),
        engine=EngineIdentity(name="pymupdf", detail="1.28.2"),
        pages=(PageText(number=1, text="one"), PageText(number=2, text="two")),
        elapsed_ms=12,
        warm_ms=warm_ms,
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("the queue never reached the expected state")


class BlockingReader:
    """A reader that announces its stage, then waits until the test lets it finish."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.content_seen: list[bytes] = []
        self.clients: list[str] = []
        self.names: list[str] = []

    def __call__(self, content, client, on_stage, name, sink=None) -> Reading:
        self.content_seen.append(content)
        self.clients.append(client)
        self.names.append(name)
        on_stage("routing")
        self.started.set()
        assert self.release.wait(5), "the test never released the reader"
        on_stage("reading")
        return _reading()


@contextmanager
def _queue(read, limits: DemoLimits | None = None, retained_jobs: int = 32):
    queue = ReadingQueue(read, limits, retained_jobs=retained_jobs)
    try:
        yield queue
    finally:
        queue.close()


def _job(queue: ReadingQueue, job_id: str):
    job = queue.job(job_id)
    assert job is not None, "the queue forgot a job it should still answer"
    return job


def _done(queue: ReadingQueue, job_id: str) -> dict:
    _wait_until(lambda: queue.job(job_id) is not None and queue.job(job_id).stage == "done")
    return _job(queue, job_id).answer()


def test_the_queue_reads_one_document_at_a_time_and_stages_each_job() -> None:
    reader = BlockingReader()
    with _queue(reader) as queue:
        first = queue.submit(b"first", "203.0.113.7", "first.pdf")
        assert reader.started.wait(5)
        second = queue.submit(b"second", "203.0.113.7", "second.pdf")

        assert queue.job(first.id).stage == "routing"
        assert queue.job(second.id).stage == "queued"
        assert queue.depth() == {"active": 1, "waiting": 1}

        reader.release.set()
        assert _done(queue, first.id)["outcome"] == "read"
        assert _done(queue, second.id)["outcome"] == "read"

        assert queue.depth() == {"active": 0, "waiting": 0}
        assert reader.content_seen == [b"first", b"second"], "the queue kept the second job waiting"
        assert reader.names == ["first.pdf", "second.pdf"]


def test_a_job_that_is_still_waiting_is_a_stage_with_no_outcome_yet() -> None:
    reader = BlockingReader()
    with _queue(reader) as queue:
        job = queue.submit(b"one", "203.0.113.7", "mine.pdf")
        assert reader.started.wait(5)

        answer = _job(queue, job.id).answer()

        assert answer["stage"] == "routing"
        assert answer["outcome"] is None
        assert answer["refusal"] is None
        assert answer["failure"] is None
        assert answer["reading"] is None
        assert answer["elapsed_ms"] is None and answer["warmed_ms"] is None
        assert answer["job"] == job.id
        reader.release.set()


def test_a_job_is_never_shown_finished_before_its_answer_is_stored() -> None:
    """The one window the page could see: a reader that says ``done`` and then takes its time to return.

    A poll in that gap must keep seeing the last stage the read really reached. ``done`` with a null
    outcome would stop the page polling and tell a visitor their job finished without an answer.
    """
    answered = threading.Event()
    holding = threading.Event()

    def reader(content, client, on_stage, name, sink=None):
        on_stage("routing")
        on_stage("reading")
        on_stage("done")
        answered.set()
        assert holding.wait(5), "the test never let the reader return"
        return _reading()

    with _queue(reader) as queue:
        job = queue.submit(b"one", "203.0.113.7", "mine.pdf")
        assert answered.wait(5)

        answer = _job(queue, job.id).answer()

        assert answer["stage"] == "reading", "the queue publishes done only together with the outcome"
        assert answer["outcome"] is None
        holding.set()
        assert _done(queue, job.id)["outcome"] == "read"


def test_a_full_waiting_list_is_refused_with_the_busy_kind() -> None:
    reader = BlockingReader()
    with _queue(reader, DemoLimits(queue_depth=1)) as queue:
        first = queue.submit(b"first", "203.0.113.7")
        assert reader.started.wait(5)
        queue.submit(b"second", "203.0.113.7")

        with pytest.raises(QueueFull) as caught:
            queue.submit(b"third", "203.0.113.7")

        assert caught.value.kind == "busy"
        assert "reading" in caught.value.reason
        assert queue.job(first.id) is not None, "a refused submission never displaces the running job"
        assert queue.depth() == {"active": 1, "waiting": 1}
        reader.release.set()


def test_a_finished_job_is_still_readable_for_a_late_poll() -> None:
    with _queue(lambda content, client, on_stage, name, sink=None: _reading()) as queue:
        job = queue.submit(b"one", "203.0.113.7")

        answer = _done(queue, job.id)

        assert answer["stage"] == "done"
        assert queue.job(job.id) is not None, "an idle page polls the job after the worker moved on"


def test_only_the_most_recent_finished_jobs_are_kept() -> None:
    """One bound, one eviction: forgetting a job forgets its document too, because the job holds it."""
    with _queue(lambda content, client, on_stage, name, sink=None: _reading(), retained_jobs=1) as queue:
        first = queue.submit(b"one", "203.0.113.7")
        _done(queue, first.id)
        second = queue.submit(b"two", "203.0.113.7")
        _done(queue, second.id)

        assert queue.job(first.id) is None, "the demo keeps no job state beyond the most recent ones"
        assert queue.job(second.id) is not None
        assert [(page.number, page.text) for page in queue.job(second.id).document().pages] == [
            (1, "one"),
            (2, "two"),
        ], "the retained job still answers its document; there is no second bound to outlive it"


def test_a_read_job_carries_the_decision_the_pages_and_the_measured_warm_up() -> None:
    with _queue(lambda content, client, on_stage, name, sink=None: _reading(warm_ms=2000)) as queue:
        job = queue.submit(b"one", "203.0.113.7", "mine.pdf")

        answer = _done(queue, job.id)

    assert set(answer) == {
        "job",
        "stage",
        "outcome",
        "elapsed_ms",
        "warmed_ms",
        "refusal",
        "failure",
        "reading",
    }
    assert answer["job"] == job.id
    assert answer["outcome"] == "read"
    assert answer["elapsed_ms"] == 12
    assert answer["warmed_ms"] == 2000
    assert answer["refusal"] is None
    assert answer["failure"] is None
    assert answer["reading"] == {
        "lane": "text-layer",
        "reason": "the text layer is healthy",
        "features": FEATURES,
        "thresholds": THRESHOLDS,
        "engine": "pymupdf@1.28.2",
        "page_count": 2,
        "character_count": 6,
        "pages": [{"number": 1, "text": "one"}, {"number": 2, "text": "two"}],
    }


def test_a_refused_job_carries_the_gates_kind_and_reason() -> None:
    refusal = Refusal(
        kind="page-cap",
        reason="this document has 30 pages; the demo reads up to 25",
        plan=PLAN,
        thresholds=OcrRoutingPolicy(),
    )
    with _queue(lambda content, client, on_stage, name, sink=None: refusal) as queue:
        answer = _done(queue, queue.submit(b"one", "203.0.113.7").id)

    assert answer["stage"] == "done"
    assert answer["outcome"] == "refused"
    assert answer["refusal"] == {"kind": "page-cap", "reason": refusal.reason}
    assert answer["failure"] is None
    assert answer["reading"] is None, "a refusal shows no page text, because none was read"
    assert answer["elapsed_ms"] is None


def test_a_failed_job_quotes_the_lane_and_the_reason() -> None:
    failure = FailedReading(
        kind="failed",
        lane="ocr",
        reason="HTTP 500 from the OCR engine",
        plan=PLAN,
        thresholds=OcrRoutingPolicy(),
    )
    with _queue(lambda content, client, on_stage, name, sink=None: failure) as queue:
        answer = _done(queue, queue.submit(b"one", "203.0.113.7").id)

    assert answer["outcome"] == "failed"
    assert answer["failure"] == {"kind": "failed", "lane": "ocr", "reason": failure.reason}
    assert answer["refusal"] is None
    assert answer["reading"] is None
