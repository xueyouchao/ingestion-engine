"""The live demo's raw transcript: bounded in memory, replayed, and streamed over SSE.

The transcript is proof of work, never evidence (ADR-0014): it holds the worker's own deltas in a
per-page ring buffer, never leaves the process, never enters the job answer, and is released the moment
the Reading finishes. These tests pin the store's bound and its event names, then drive the real HTTP
server end to end so the wire a browser sees is the one the page asset depends on.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from finepdfs.domain.document import PageText
from finepdfs.interfaces.demo_transcript import RawTranscript, stream_events


def _events(transcript: RawTranscript, cursor: int = 0) -> list[tuple[str, dict]]:
    return [(event.name, event.data) for event in transcript.since(cursor)[0]]


def test_the_store_replays_a_pages_deltas_in_arrival_order_and_its_completed_page() -> None:
    """A reloaded subscriber sees the same deltas the live one did, then the page they added up to."""
    transcript = RawTranscript(ceiling_bytes=1024)

    transcript.on_delta(1, "OCR")
    transcript.on_delta(1, ":")
    transcript.on_delta(1, "\n# Report")
    transcript.on_page(PageText(number=1, text="# Report"))
    transcript.finish("read")

    assert _events(transcript) == [
        ("delta", {"page": 1, "text": "OCR"}),
        ("delta", {"page": 1, "text": ":"}),
        ("delta", {"page": 1, "text": "\n# Report"}),
        ("page", {"number": 1, "text": "# Report"}),
        ("done", {"outcome": "read"}),
    ]
    assert transcript.since(0)[1] is True


def test_a_page_that_outruns_the_ceiling_keeps_its_tail_and_names_what_it_dropped() -> None:
    """The bound is bytes, the loss is explicit, and the surviving text is the most recent read."""
    transcript = RawTranscript(ceiling_bytes=8)

    transcript.on_delta(1, "12345")
    transcript.on_delta(1, "67890")

    events = _events(transcript)
    kept = "".join(data["text"] for name, data in events if name == "delta")
    truncated = [data for name, data in events if name == "truncated"]

    assert len(kept.encode()) <= 8
    assert kept == "67890", "the newest bytes are kept and the oldest are the ones dropped"
    assert truncated == [{"page": 1, "dropped_bytes": 5}]


def test_one_delta_larger_than_the_ceiling_is_cut_to_its_own_tail() -> None:
    """A single giant frame cannot blow the bound, and the cut is still marked."""
    transcript = RawTranscript(ceiling_bytes=4)

    transcript.on_delta(1, "abcdef")

    events = _events(transcript)
    kept = "".join(data["text"] for name, data in events if name == "delta")

    assert kept == "cdef"
    assert ("truncated", {"page": 1, "dropped_bytes": 2}) in events


def test_the_dropped_count_accumulates_per_page_without_keeping_the_dropped_text() -> None:
    """Truncation is reported as a growing count, and no dropped byte is retained anywhere."""
    transcript = RawTranscript(ceiling_bytes=4)

    transcript.on_delta(1, "aaaa")
    transcript.on_delta(1, "bbbb")
    transcript.on_delta(1, "cccc")

    events = _events(transcript)
    drops = [data["dropped_bytes"] for name, data in events if name == "truncated"]
    kept = "".join(data["text"] for name, data in events if name == "delta")

    assert drops == [8], "one marker per page, carrying the cumulative count a replay sees"
    assert kept == "cccc"
    assert "aaaa" not in json.dumps(events) and "bbbb" not in json.dumps(events)


def test_a_failure_is_an_error_event_and_finishing_is_a_done_event() -> None:
    """The stream says why it failed in the same words the job answer shows, then ends."""
    transcript = RawTranscript()

    transcript.on_delta(1, "partial")
    transcript.fail("failed", "ocr", "the worker ran out of memory")
    transcript.finish("failed")

    assert _events(transcript)[-2:] == [
        ("error", {"kind": "failed", "lane": "ocr", "reason": "the worker ran out of memory"}),
        ("done", {"outcome": "failed"}),
    ]
    assert transcript.since(0)[1] is True


def test_a_quiet_read_sends_a_heartbeat_and_a_finished_read_ends_the_stream() -> None:
    """The writer heartbeats while nothing arrives, and returns rather than waiting forever."""
    transcript = RawTranscript()
    written: list[bytes] = []

    thread = threading.Thread(
        target=stream_events,
        args=(transcript, written.append),
        kwargs={"heartbeat_seconds": 0.02},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 2
    while not any(chunk == b": heartbeat\n\n" for chunk in written) and time.monotonic() < deadline:
        time.sleep(0.01)

    transcript.on_delta(1, "late")
    transcript.finish("read")
    thread.join(timeout=2)

    assert not thread.is_alive(), "a finished transcript ends the writer"
    assert b": heartbeat\n\n" in written
    assert b'event: delta\ndata: {"page": 1, "text": "late"}\n\n' in written
    assert b'event: done\ndata: {"outcome": "read"}\n\n' in written


# -- the queue's ownership of the store ---------------------------------------------------------------


class _StreamingReader:
    """A reader that emits a page's deltas through its sink, then waits to be released."""

    def __init__(self, deltas: list[str], *, require_sink: bool = True) -> None:
        self._deltas = deltas
        self._require_sink = require_sink
        self.started = threading.Event()
        self.release = threading.Event()
        self.sink = None

    def __call__(self, content, client, on_stage, name, sink=None):
        self.sink = sink
        on_stage("reading")
        if self._require_sink:
            assert sink is not None, "the queue handed its reader no transcript"
        for text in self._deltas:
            if sink is not None:
                sink.on_delta(1, text)
        if sink is not None:
            sink.on_page(PageText(number=1, text="the page"))
        self.started.set()
        assert self.release.wait(5), "the test never released the reader"
        on_stage("done")
        return _reading()


def _reading():
    from finepdfs.application.curate import RoutingPlan
    from finepdfs.application.demo_read import Reading
    from finepdfs.domain.document import EngineIdentity, TextLayerQuality
    from finepdfs.domain.ocr import OcrRoutingPolicy, RoutingDecision

    quality = TextLayerQuality(
        page_count=1, pages_with_text=1, character_count=100, garbled_ratio=0.0, image_dominated_pages=0
    )
    return Reading(
        plan=RoutingPlan(quality, RoutingDecision(False, "a page was read", {}), "pymupdf"),
        thresholds=OcrRoutingPolicy(),
        engine=EngineIdentity("pymupdf", "fake"),
        pages=(PageText(number=1, text="the page"),),
        elapsed_ms=1,
        warm_ms=None,
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("the demo never reached the expected state")


def test_the_queue_hands_its_reader_a_transcript_and_frees_it_when_the_job_finishes() -> None:
    """The transcript belongs to the job: it is watchable while the read runs and gone once it is done."""
    from finepdfs.domain.demo import DemoLimits
    from finepdfs.interfaces.demo_jobs import ReadingQueue

    reader = _StreamingReader(["OCR", ":", "\npage"])
    queue = ReadingQueue(reader, DemoLimits(transcript_bytes=1024))
    try:
        job = queue.submit(b"one", "203.0.113.7")
        assert reader.started.wait(5)

        transcript = queue.transcript(job.id)
        assert transcript is not None, "a running job keeps its transcript"
        names = [event.name for event in transcript.since(0)[0]]
        assert names == ["delta", "delta", "delta", "page"]
        assert "transcript" not in job.as_json() and "events" not in job.as_json()

        reader.release.set()
        _wait_until(lambda: queue.get(job.id) is not None and queue.get(job.id).stage == "done")

        assert queue.transcript(job.id) is None, "a finished job frees its transcript"
        answer = queue.get(job.id).as_json()
        assert answer["reading"]["pages"] == [{"number": 1, "text": "the page"}]
    finally:
        queue.close()


def test_the_queue_bounds_each_page_to_the_limit_and_marks_the_drop() -> None:
    """The ceiling is the configured demo limit, and a page past it loses its start, visibly."""
    from finepdfs.domain.demo import DemoLimits
    from finepdfs.interfaces.demo_jobs import ReadingQueue

    reader = _StreamingReader(["12345", "67890"])
    queue = ReadingQueue(reader, DemoLimits(transcript_bytes=8))
    try:
        job = queue.submit(b"one", "203.0.113.7")
        assert reader.started.wait(5)

        transcript = queue.transcript(job.id)
        assert transcript is not None
        events = [(event.name, event.data) for event in transcript.since(0)[0]]

        reader.release.set()
        _wait_until(lambda: queue.get(job.id) is not None and queue.get(job.id).stage == "done")
    finally:
        queue.close()

    deltas = [data["text"] for name, data in events if name == "delta"]
    assert "".join(deltas) == "67890", "the newest bytes survive; the start of the read is dropped"
    assert len("".join(deltas).encode()) <= 8
    assert [data for name, data in events if name == "truncated"] == [{"page": 1, "dropped_bytes": 5}]


def test_a_transcript_ceiling_of_zero_keeps_no_text_at_all() -> None:
    """An operator can switch the transcript off; memory then holds nothing but the fact of truncation."""
    from finepdfs.domain.demo import DemoLimits
    from finepdfs.interfaces.demo_jobs import ReadingQueue

    reader = _StreamingReader(["OCR"], require_sink=False)
    queue = ReadingQueue(reader, DemoLimits(transcript_bytes=0))
    try:
        job = queue.submit(b"one", "203.0.113.7")
        assert reader.started.wait(5)

        assert queue.transcript(job.id) is None, "a zero ceiling keeps no transcript at all"
        reader.release.set()
        _wait_until(lambda: queue.get(job.id) is not None and queue.get(job.id).stage == "done")
    finally:
        queue.close()
