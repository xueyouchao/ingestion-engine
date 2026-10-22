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
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlsplit

from finepdfs.domain.demo import DemoLimits
from finepdfs.domain.document import PageText
from finepdfs.interfaces import demo_server
from finepdfs.interfaces.demo_transcript import RawTranscript, stream_events
from tests.fake_streaming_worker import FakeStreamingWorker


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


# -- the job's ownership of its transcript ------------------------------------------------------------


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

        transcript = queue.job(job.id).transcript()
        assert transcript is not None, "a running job keeps its transcript"
        names = [event.name for event in transcript.since(0)[0]]
        assert names == ["delta", "delta", "delta", "page"]
        assert "transcript" not in job.answer() and "events" not in job.answer()

        reader.release.set()
        _wait_until(lambda: queue.job(job.id) is not None and queue.job(job.id).stage == "done")

        assert queue.job(job.id).transcript() is None, "a finished job frees its transcript"
        answer = queue.job(job.id).answer()
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

        transcript = queue.job(job.id).transcript()
        assert transcript is not None
        events = [(event.name, event.data) for event in transcript.since(0)[0]]

        reader.release.set()
        _wait_until(lambda: queue.job(job.id) is not None and queue.job(job.id).stage == "done")
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

        assert queue.job(job.id).transcript() is None, "a zero ceiling keeps no transcript at all"
        reader.release.set()
        _wait_until(lambda: queue.job(job.id) is not None and queue.job(job.id).stage == "done")
    finally:
        queue.close()


# -- the real HTTP server, end to end ------------------------------------------------------------------


OCR_ENGINE = "paddleocr-vl"
WORKER_ADDRESS = "<the OCR worker>"


class _ScannedProbe:
    """The text-layer probe's answer for a scan: one page, no text, so the OCR lane is chosen."""

    def measure(self, content: bytes):
        from finepdfs.domain.document import TextLayerQuality

        return TextLayerQuality(
            page_count=1, pages_with_text=0, character_count=0, garbled_ratio=0.0, image_dominated_pages=1
        )


class _ReadyEndpoint:
    def ready(self) -> bool:
        return True


class _Lease:
    def __init__(self, engine) -> None:
        self._engine = engine

    def __enter__(self):
        return self._engine, _ReadyEndpoint()

    def __exit__(self, *exc_info: object) -> None:
        return None


def _scanned_pdf() -> bytes:
    """A real one-page PDF the adapter can render, so the worker sees the real streaming request."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 120), "a synthetic scan")
    content = document.tobytes()
    document.close()
    return content


def _server_app(worker: FakeStreamingWorker, limits=None):
    """The real use case over the shared fake worker, composed exactly as the demo server composes it."""
    from finepdfs.application.curate import CurationPolicy
    from finepdfs.application.demo_read import DemoRead
    from finepdfs.domain.demo import DemoLimits, SpendGatePolicy
    from finepdfs.domain.ocr import OcrRoutingPolicy
    from finepdfs.infrastructure.demo.ledger import InMemoryGateLedger
    from finepdfs.infrastructure.engines.paddleocr_vl import PaddleOcrVlEngine
    from finepdfs.infrastructure.engines.vlm_chat import ChatEndpoint, VlmChatTransport
    from finepdfs.interfaces.demo_server import DemoApp

    settings = limits if limits is not None else DemoLimits()
    environ = {"FINEPDFS_ENGINE_BASE_URL": worker.base_url, "FINEPDFS_ENGINE_MODEL": "PaddleOCR-VL-1.6"}
    engine = PaddleOcrVlEngine(
        transport=VlmChatTransport(ChatEndpoint(base_url=worker.base_url, model="PaddleOCR-VL-1.6")),
        dpi=72,
    )
    ledger = InMemoryGateLedger(clock=datetime.now, limits=settings)
    read = DemoRead(
        probe=_ScannedProbe(),
        engines={},
        policy=CurationPolicy(engine_name=OCR_ENGINE, routing=OcrRoutingPolicy()),
        gates=SpendGatePolicy(settings),
        ledger=ledger,
        worker=lambda: _Lease(engine),
        warm_timeout_seconds=settings.warm_timeout_seconds,
        poll_interval_seconds=settings.poll_interval_seconds,
        redact=lambda message: demo_server.redact(message, environ),
    ).read
    return DemoApp(limits=settings, engine_name=OCR_ENGINE, environ=environ, ledger=ledger, read=read), ledger


def _reader_app(read, limits=None):
    from finepdfs.interfaces.demo_server import DemoApp

    settings = limits if limits is not None else DemoLimits()
    return DemoApp(limits=settings, environ={}, read=read)


@contextmanager
def _serving(app):
    from finepdfs.interfaces import demo_server

    server = demo_server.make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        app.close()


def _post(base: str, body: bytes) -> tuple[int, dict]:
    import urllib.request

    request = urllib.request.Request(
        base + "/api/read", data=body, method="POST", headers={"Content-Type": "application/pdf"}
    )
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


def _json(url: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def _wait_for_done(base: str, job: str) -> dict:
    answer: dict = {}
    _wait_until(lambda: _json(base + f"/api/read/{job}").get("stage") == "done")

    answer.update(_json(base + f"/api/read/{job}"))
    return answer


class _Subscriber:
    """One SSE subscriber: the response headers, then events parsed off the wire."""

    def __init__(self, base: str, job: str) -> None:
        import http.client

        parts = urlsplit(base)
        self._connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        self._connection.request("GET", f"/api/read/{job}/events")
        self.response = self._connection.getresponse()
        self.content_type = self.response.getheader("Content-Type", "")
        self.cache_control = self.response.getheader("Cache-Control", "")

    def next_event(self) -> tuple[str | None, dict | None] | None:
        """The next event, ``("comment", {"line": ...})`` for a heartbeat, or ``None`` at end of stream."""
        name: str | None = None
        data: dict | None = None
        while True:
            line = self.response.readline()
            if not line:
                return None
            text = line.decode("utf-8").rstrip("\n")
            if text.startswith(":"):
                return "comment", {"line": text}
            if text.startswith("event: "):
                name = text.removeprefix("event: ")
            elif text.startswith("data: "):
                data = json.loads(text.removeprefix("data: "))
            elif text == "":
                if name is None and data is None:
                    continue
                return name, data

    def close(self) -> None:
        self._connection.close()


def _streamed_events(subscriber: _Subscriber) -> list[tuple[str | None, dict | None]]:
    events: list = []
    while True:
        event = subscriber.next_event()
        if event is None:
            return events
        events.append(event)
        if event[0] == "done":
            return events


def _http_error(url: str) -> tuple[int, dict]:
    """Ask for a URL that must be refused, and answer its status and JSON body."""
    try:
        with urllib.request.urlopen(url) as response:
            raise AssertionError(f"expected an HTTP error, got {response.status}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_the_real_server_streams_the_raw_read_and_a_reload_replays_it_from_the_start() -> None:
    """Upload, watch deltas and a page arrive, reload halfway through, and converge on one answer."""
    from tests.fake_streaming_worker import delta_text, recorded_frames

    frames = recorded_frames()
    deltas = [text for text in map(delta_text, frames) if text]
    hold = threading.Event()
    with FakeStreamingWorker(hold_after=5, hold=hold) as worker:
        app, ledger = _server_app(worker)
        with _serving(app) as base:
            status, answer = _post(base, _scanned_pdf())
            assert status == 202
            job = answer["job"]

            first = _Subscriber(base, job)
            assert first.content_type.startswith("text/event-stream")
            assert first.cache_control == "no-store"
            watched: list = []
            while len([event for event in watched if event[0] == "delta"]) < 4:
                watched.append(first.next_event())
            first.close()

            reloaded = _Subscriber(base, job)
            replayed: list = []
            while len([event for event in replayed if event[0] == "delta"]) < 4:
                replayed.append(reloaded.next_event())
            assert replayed == watched, "a reload is replayed from the start, delta for delta"

            hold.set()
            rest = _streamed_events(reloaded)
            final = _wait_for_done(base, job)

    assert [data["text"] for name, data in watched if name == "delta"] == deltas[:4]
    page_events = [data for name, data in rest if name == "page"]
    assert page_events == [{"number": 1, "text": final["reading"]["pages"][0]["text"]}]
    assert ("done", {"outcome": "read"}) in rest
    assert set(final) == {
        "job",
        "stage",
        "outcome",
        "elapsed_ms",
        "warmed_ms",
        "refusal",
        "failure",
        "reading",
    }, "the job answer keeps exactly its old shape"
    encoded = json.dumps(final)
    assert "transcript" not in encoded and "<html" not in encoded
    assert worker.bodies[0]["stream"] is True
    assert ledger.usage("127.0.0.1").reads_in_window == 1
    assert ledger.usage("127.0.0.1").ocr_pages_today == 1
    assert app.job(job).transcript() is None, "the finished job keeps no transcript"


def test_closing_the_connection_mid_stream_charges_exactly_one_read_and_its_pages() -> None:
    """A disconnect is not a refund (ADR-0012): the GPU read the page, so it is charged once."""
    hold = threading.Event()
    with FakeStreamingWorker(hold_after=3, hold=hold) as worker:
        app, ledger = _server_app(worker)
        with _serving(app) as base:
            _, answer = _post(base, _scanned_pdf())
            job = answer["job"]

            subscriber = _Subscriber(base, job)
            seen: list = []
            while len([event for event in seen if event[0] == "delta"]) < 2:
                seen.append(subscriber.next_event())
            subscriber.close()

            hold.set()
            final = _wait_for_done(base, job)

    usage = ledger.usage("127.0.0.1")
    assert final["outcome"] == "read"
    assert (usage.reads_in_window, usage.ocr_pages_today) == (1, 1)
    assert len(worker.bodies) == 1


def test_a_failed_read_ends_the_stream_with_its_kind_lane_and_redacted_words() -> None:
    """A worker failure is an ``error`` event in the same words the job answer carries, then the end."""
    hold = threading.Event()
    with FakeStreamingWorker(
        fail_after=5, hold_after=3, hold=hold, error="the model ran out of memory"
    ) as worker:
        app, _ = _server_app(worker)
        with _serving(app) as base:
            _, answer = _post(base, _scanned_pdf())
            job = answer["job"]
            subscriber = _Subscriber(base, job)
            watched: list = []
            while len([event for event in watched if event[0] == "delta"]) < 2:
                watched.append(subscriber.next_event())
            hold.set()
            rest = _streamed_events(subscriber)
            final = _wait_for_done(base, job)

    errors = [data for name, data in rest if name == "error"]
    assert len(errors) == 1
    assert errors[0]["kind"] == "failed"
    assert errors[0]["lane"] == "ocr"
    assert "the model ran out of memory" in errors[0]["reason"]
    assert worker.base_url not in errors[0]["reason"], "the worker's address is redacted"
    assert WORKER_ADDRESS in errors[0]["reason"]
    assert final["outcome"] == "failed"
    assert final["failure"]["reason"] == errors[0]["reason"]


def test_a_quiet_read_heartbeats_and_an_unknown_id_is_a_plain_http_error() -> None:
    """A quiet stream says it is alive; an id this process never issued is never a stream."""
    reader = _StreamingReader([], require_sink=True)
    app = _reader_app(reader, DemoLimits(heartbeat_seconds=0.02))
    settings = app.limits
    assert settings.heartbeat_seconds == 0.02
    try:
        with _serving(app) as base:
            status, not_found = _http_error(base + "/api/read/no-such-job/events")
            assert status == 404
            assert "no job" in not_found["error"]

            _, answer = _post(base, b"%PDF-1.7 an upload")
            job = answer["job"]
            assert reader.started.wait(5)
            subscriber = _Subscriber(base, job)
            first = subscriber.next_event()
            while first is not None and first[0] != "comment":
                first = subscriber.next_event()
            assert first is not None, "a quiet read sends a heartbeat"
            assert first[1]["line"] == ": heartbeat"
            reader.release.set()
            rest = _streamed_events(subscriber)
            assert ("done", {"outcome": "read"}) in rest

            status, finished = _http_error(base + f"/api/read/{job}/events")
            assert status == 410
            assert "finished" in finished["error"]
    finally:
        app.close()

