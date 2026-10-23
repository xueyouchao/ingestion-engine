"""The live demo's middle pane: the raw transcript, fed by the events endpoint.

There is no browser in this environment, so these tests pin the page asset's script in two honest
ways: the asset's own source (no HTML parsing on the transcript path, one event stream per job), and
the page's pure event functions run under Node against event lists the real HTTP endpoint produced.
No DOM is invented in Python: the functions exercised are the ones the browser runs, and what the
browser would receive is the wire the SSE tests already parse.
"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlsplit

import pytest

from finepdfs.domain.document import PageText
from finepdfs.interfaces import demo_page, demo_server

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node runs the page's pure script functions")

HOSTILE = '<script>alert("own")</script><img src=x onerror="alert(1)">'


def _function(name: str) -> str:
    """The page's source for one top-level function, so Node can run exactly what the browser does."""
    found = re.search(rf"(function {name}\(.*?\n\}})", demo_page.DEMO_HTML, re.S)
    assert found is not None, f"the page asset defines {name}()"
    return found.group(1)


def _drive(events: list[tuple[str, dict]], functions: tuple[str, ...], body: str) -> dict:
    """Run one snippet of the page's own JS over an event list, and read its JSON answer."""
    source = "\n".join(_function(name) for name in functions)
    script = f"{source}\nconst events = {json.dumps(events)};\n{body}\n"
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _apply(events: list[tuple[str, dict]], body: str = "") -> dict:
    default = (
        'const model = rawModel();\n'
        'for (const [name, data] of events) applyRawEvent(model, name, data);\n'
        'console.log(JSON.stringify({model: model, pages: rawPages(model), note: rawNote(model),'
        ' text: rawText(model)}));'
    )
    return _drive(events, ("rawModel", "applyRawEvent", "rawPages", "rawNote", "rawText"), default + body)


def test_the_raw_pane_is_fed_by_the_events_endpoint_and_sets_it_as_text_only() -> None:
    """The pane is one section fed by EventSource, and the transcript path never builds markup."""
    page = demo_page.DEMO_HTML

    assert 'id="raw"' in page and 'id="raw-transcript"' in page and 'id="raw-copy"' in page
    assert 'new EventSource("/api/read/" + encodeURIComponent(jobId) + "/events")' in page
    assert "renderRaw" in page

    # Ticket 37 gives the page its one insertion: the rendered document's fragment, whose own test
    # (`test_demo_page_document.py`) pins that it is the only innerHTML in the asset. This pane's path
    # is still text only, so the assertion moves from the whole page to the transcript's own functions.
    assert page.count("innerHTML") == 1, "the raw pane adds no second place that parses text"
    for forbidden in ("outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in page, f"the page never parses text on the transcript path ({forbidden})"
    for transcript_function in ("renderRaw", "openRawStream", "settleRaw", "copyRaw", "applyRawEvent", "node"):
        assert "innerHTML" not in _function(transcript_function), (
            f"{transcript_function} writes text, never markup"
        )
    render = _function("renderRaw")
    assert "textContent" in render, "the raw text is assigned as text"
    assert "replaceChildren" in render
    assert "element.textContent = String(text);" in _function("node"), "every node's text is its textContent"


# A DOM stand-in small enough to run the pane's own renderer in Node: elements hold their children and
# text, and a full rebuild is counted, so the test can say how often the pane was rebuilt.
FAKE_DOM = """
function element(tag) {
  return {
    tagName: tag,
    className: "",
    dataset: {},
    children: [],
    rebuilds: 0,
    _text: "",
    classList: { remove() {}, add() {} },
    setAttribute() {},
    append(...kids) { this.children.push(...kids); },
    replaceChildren() { this.children = []; this.rebuilds += 1; },
    set textContent(value) { this._text = String(value); },
    get textContent() {
      return this._text + this.children.map((child) => (child.textContent === undefined ? "" : child.textContent)).join("");
    },
  };
}
const panes = {};
for (const id of ["raw", "raw-note", "raw-copy", "raw-transcript"]) panes[id] = element("div");
const container = panes["raw-transcript"];
globalThis.document = {
  getElementById: (id) => panes[id],
  createElement: (tag) => element(tag),
  createTextNode: (text) => ({ textContent: String(text), children: [] }),
};
globalThis.byId = (id) => panes[id];
"""


def test_a_delta_appends_to_the_page_it_belongs_to_and_only_a_new_page_rebuilds() -> None:
    """A streamed token costs one append: the page's text grows once, and no page is rebuilt for it."""
    events = [
        ("delta", {"page": 1, "text": "one "}),
        ("delta", {"page": 1, "text": "two "}),
        ("delta", {"page": 1, "text": "three"}),
    ]
    functions = ("rawModel", "applyRawEvent", "appendRawEvent", "rawPages", "rawNote", "rawText", "rawTruncation", "node", "renderRaw")
    source = "\n".join(_function(name) for name in functions)
    script = (
        f"{FAKE_DOM}\n{source}\nconst events = {json.dumps(events)};\n"
        "const model = rawModel();\n"
        "const steps = [];\n"
        "let firstArticle = null;\n"
        "for (const [name, data] of events) {\n"
        "  const appended = appendRawEvent(model, name, data);\n"
        "  if (!appended) renderRaw(model);\n"
        "  if (firstArticle === null) firstArticle = container.children[0];\n"
        "  steps.push({appended: appended, rebuilds: container.rebuilds});\n"
        "}\n"
        "const sameArticle = container.children[0] === firstArticle;\n"
        "const truncated = appendRawEvent(model, \"truncated\", {page: 1, dropped_bytes: 4});\n"
        "if (!truncated) renderRaw(model);\n"
        "console.log(JSON.stringify({\n"
        "  steps: steps,\n"
        "  sameArticle: sameArticle,\n"
        "  pre: container.children[0].children[container.children[0].children.length - 1].textContent,\n"
        "  pages: rawPages(model),\n"
        "  truncatedRebuilt: truncated === false && container.rebuilds === 2,\n"
        "}));"
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)

    assert rendered["steps"][0] == {"appended": False, "rebuilds": 1}, "a page first appearing is drawn"
    assert rendered["steps"][1:] == [{"appended": True, "rebuilds": 1}, {"appended": True, "rebuilds": 1}], (
        "the later deltas append to that page, with no second rebuild"
    )
    assert rendered["sameArticle"] is True, "the page's element is the one drawn when it first appeared"
    assert rendered["pre"] == "one two three", "the pane's text is the concatenation the model holds"
    assert rendered["pages"] == [{"number": 1, "text": "one two three", "dropped": 4}], (
        "the model kept every delta's text, and the truncation marker is the last thing applied"
    )
    assert rendered["truncatedRebuilt"] is True, "a truncation marker changes the structure, so it redraws"


@requires_node
def test_a_hostile_delta_reaches_the_raw_pane_as_text() -> None:
    """A delta is the worker's bytes, not markup: it survives verbatim and the renderer sets text."""
    result = _apply(
        [("delta", {"page": 1, "text": HOSTILE}), ("done", {"outcome": "read"})]
    )

    assert result["pages"] == [{"number": 1, "text": HOSTILE, "dropped": 0}]
    assert result["text"] == HOSTILE
    assert result["model"]["finished"] is True
    assert result["model"]["outcome"] == "read"


# -- the real endpoint, end to end -------------------------------------------------------------------
#
# The page's stream lifecycle is exercised against the real ThreadingHTTPServer and the same fake
# worker the SSE tests use. The event lists these tests hand to the page's own JS are the ones the
# browser would have received, parsed exactly as the page parses them.


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
        pages=(PageText(number=1, text="a page read whole"),),
        elapsed_ms=1,
        warm_ms=None,
    )


class _QuietReader:
    """A lane that returns its page whole: no delta ever reaches the sink, as the text layer does."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, content, client, on_stage, name, sink=None):
        on_stage("reading")
        if sink is not None:
            sink.on_page(PageText(number=1, text="a page read whole"))
        self.started.set()
        assert self.release.wait(5), "the test never released the quiet reader"
        on_stage("done")
        return _reading()


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
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 120), "a synthetic scan")
    content = document.tobytes()
    document.close()
    return content


def _ocr_app(worker):
    """The real streaming OCR composition, over the shared fake worker."""
    from finepdfs.application.curate import CurationPolicy
    from finepdfs.application.demo_read import DemoRead
    from finepdfs.domain.demo import DemoLimits, SpendGatePolicy
    from finepdfs.domain.ocr import OcrRoutingPolicy
    from finepdfs.infrastructure.demo.ledger import InMemoryGateLedger
    from finepdfs.infrastructure.engines.paddleocr_vl import PaddleOcrVlEngine
    from finepdfs.infrastructure.engines.vlm_chat import ChatEndpoint, VlmChatTransport
    from finepdfs.interfaces.demo_server import DemoApp

    settings = DemoLimits()
    environ = {"FINEPDFS_ENGINE_BASE_URL": worker.base_url, "FINEPDFS_ENGINE_MODEL": "PaddleOCR-VL-1.6"}
    engine = PaddleOcrVlEngine(
        transport=VlmChatTransport(ChatEndpoint(base_url=worker.base_url, model="PaddleOCR-VL-1.6")),
        dpi=72,
    )
    ledger = InMemoryGateLedger(clock=datetime.now, limits=settings)
    read = DemoRead(
        probe=_ScannedProbe(),
        engines={},
        policy=CurationPolicy(engine_name="paddleocr-vl", routing=OcrRoutingPolicy()),
        gates=SpendGatePolicy(settings),
        ledger=ledger,
        worker=lambda: _Lease(engine),
        warm_timeout_seconds=settings.warm_timeout_seconds,
        poll_interval_seconds=settings.poll_interval_seconds,
        redact=lambda message: message.replace(worker.base_url, "<the OCR worker>"),
    ).read
    return DemoApp(limits=settings, engine_name="paddleocr-vl", environ=environ, ledger=ledger, read=read)


class _Subscriber:
    """One SSE subscriber: the response headers, then events parsed off the wire."""

    def __init__(self, base: str, job: str) -> None:
        parts = urlsplit(base)
        self._connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        self._connection.request("GET", f"/api/read/{job}/events")
        self.response = self._connection.getresponse()
        self.content_type = self.response.getheader("Content-Type", "")

    def next_event(self):
        name = None
        data = None
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


def _streamed_events(subscriber: _Subscriber) -> list[tuple[str, dict]]:
    events: list = []
    while True:
        event = subscriber.next_event()
        if event is None:
            return events
        if event[0] != "comment":
            events.append(event)
        if event[0] == "done":
            return events


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("the demo never reached the expected state")


def _wait_for_done(base: str, job: str) -> dict:
    _wait_until(lambda: _json(base + f"/api/read/{job}").get("stage") == "done")
    return _json(base + f"/api/read/{job}")


def _json(url: str) -> dict:
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def _post(base: str, body: bytes) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + "/api/read", data=body, method="POST", headers={"Content-Type": "application/pdf"}
    )
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


@contextmanager
def _serving(app):
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


@requires_node
def test_a_quiet_stream_waits_and_a_lane_with_nothing_to_stream_says_so() -> None:
    """The two empty panes differ on the wire: no event yet waits; done with no delta does not stream."""
    reader = _QuietReader()
    app = demo_server.DemoApp(limits=demo_server.DemoLimits(heartbeat_seconds=0.02), environ={}, read=reader)
    try:
        with _serving(app) as base:
            status, answer = _post(base, b"%PDF-1.7 an upload")
            assert status == 202
            job = answer["job"]
            assert reader.started.wait(5)

            subscriber = _Subscriber(base, job)
            first = subscriber.next_event()
            while first is not None and first[0] != "comment":
                first = subscriber.next_event()
            assert first is not None and first[1]["line"] == ": heartbeat", "the quiet lane says it is alive"

            # What the browser holds at that moment: no delta has arrived, and the read is not done.
            assert "Waiting" in _apply([])["note"]

            reader.release.set()
            events = _streamed_events(subscriber)
            final = _wait_for_done(base, job)
    finally:
        app.close()

    assert [name for name, _ in events if name == "delta"] == [], "this lane never streams a token"
    assert events[-1] == ("done", {"outcome": "read"})
    rendered = _apply(events)
    assert rendered["pages"] == []
    assert "This lane does not stream" in rendered["note"]
    assert rendered["model"]["finished"] is True
    assert final["reading"]["pages"] == [{"number": 1, "text": "a page read whole"}]


@requires_node
def test_a_truncated_page_is_marked_in_words_beside_what_remains() -> None:
    """The server's ``truncated`` event becomes a sentence next to the retained tail, not a silent cut."""
    events = [
        ("truncated", {"page": 1, "dropped_bytes": 12}),
        ("delta", {"page": 1, "text": "the retained tail"}),
        ("done", {"outcome": "read"}),
    ]
    rendered = _apply(events)

    assert rendered["pages"] == [{"number": 1, "text": "the retained tail", "dropped": 12}]
    truncation = _drive(events, ("rawTruncation",), 'console.log(JSON.stringify(rawTruncation(1, 12)));')
    assert "Page 1" in truncation and "12" in truncation
    assert "dropped" in truncation and "bytes" in truncation, "the cut is stated in words, not left implicit"

    render = _function("renderRaw")
    assert "rawTruncation(" in render, "the sentence is what the pane shows"
    assert render.index("rawTruncation(") < render.index("raw-text"), "the marker sits next to the text"


@requires_node
def test_a_reload_mid_stream_replays_from_the_start_and_converges_on_the_jobs_answer() -> None:
    """A second subscriber (the reloaded page) is replayed from event zero and ends where the job does."""
    from tests.fake_streaming_worker import FakeStreamingWorker, delta_text, recorded_frames

    deltas = [text for text in map(delta_text, recorded_frames()) if text]
    hold = threading.Event()
    with FakeStreamingWorker(hold_after=5, hold=hold) as worker:
        app = _ocr_app(worker)
        try:
            with _serving(app) as base:
                status, answer = _post(base, _scanned_pdf())
                assert status == 202
                job = answer["job"]

                live = _Subscriber(base, job)
                watched: list = []
                while len([event for event in watched if event[0] == "delta"]) < 4:
                    watched.append(live.next_event())
                live.close()

                reloaded = _Subscriber(base, job)
                replay: list = []
                while len([event for event in replay if event[0] == "delta"]) < 4:
                    replay.append(reloaded.next_event())
                assert replay == watched, "the reload is replayed from the retained start, delta for delta"

                hold.set()
                rest = _streamed_events(reloaded)
                final = _wait_for_done(base, job)
                assert app.transcript(job) is None, "the finished job keeps no transcript"
        finally:
            app.close()

    events = [event for event in replay + rest if event[0] != "comment"]
    rendered = _apply(events)
    assert rendered["model"]["finished"] is True
    assert rendered["model"]["outcome"] == "read"
    assert rendered["text"] == "".join(deltas), "the reloaded page shows the whole raw read, nothing lost"
    assert rendered["pages"] == [{"number": 1, "text": "".join(deltas), "dropped": 0}]
    assert [data for name, data in rest if name == "page"] == [
        {"number": 1, "text": final["reading"]["pages"][0]["text"]}
    ], "the page event converges on the answer the job finally carries"


@requires_node
def test_a_failed_read_keeps_the_transcript_it_produced_and_the_failure_panel_names_the_lane() -> None:
    """An ``error`` event does not erase the raw output; the lane and the words survive to the panel."""
    from tests.fake_streaming_worker import FakeStreamingWorker

    hold = threading.Event()
    with FakeStreamingWorker(
        fail_after=5, hold_after=3, hold=hold, error="the model ran out of memory"
    ) as worker:
        app = _ocr_app(worker)
        try:
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
        finally:
            app.close()

    assert watched, "the failed read had already produced raw output"
    errors = [data for name, data in rest if name == "error"]
    assert len(errors) == 1 and errors[0]["lane"] == "ocr"
    assert "the model ran out of memory" in errors[0]["reason"]

    rendered = _apply(watched + rest)
    assert rendered["text"] != "", "the transcript it did produce is kept"
    assert rendered["pages"][0]["text"] == rendered["text"]
    assert rendered["model"]["failure"] == {"lane": "ocr", "reason": errors[0]["reason"]}
    assert rendered["model"]["finished"] is True and rendered["model"]["outcome"] == "failed"

    assert final["outcome"] == "failed"
    assert final["failure"]["lane"] == "ocr"
    assert final["failure"]["reason"] == errors[0]["reason"]
    page = demo_page.DEMO_HTML
    assert 'id="failure-lane"' in page and 'id="failure-reason"' in page
    assert '"raw"' not in _function("hideResults"), "a new outcome never hides the transcript it produced"


STREAM_STUBS = """
const state = { raw: null, rawJob: null, events: null };
let renders = 0;
function renderRaw() { renders += 1; }
const opened = [];
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closed = false;
    opened.push(this);
  }
  addEventListener(name, handler) {
    (this.listeners[name] = this.listeners[name] || []).push(handler);
  }
  close() { this.closed = true; }
  fire(name, event) { for (const handler of this.listeners[name] || []) handler(event); }
}
globalThis.EventSource = FakeEventSource;
"""


@requires_node
def test_a_stale_streams_done_and_error_leave_the_new_jobs_stream_and_pane_alone() -> None:
    """A handler from the job an upload replaced still fires; it may not close or write the new job's stream."""
    source = "\n".join(
        _function(name)
        for name in ("rawModel", "applyRawEvent", "appendRawEvent", "closeRawStream", "openRawStream")
    )
    script = (
        f"{STREAM_STUBS}\n{source}\n"
        "openRawStream('old');\n"
        "const older = opened[0];\n"
        "older.fire('delta', {data: JSON.stringify({page: 1, text: 'the old job text'})});\n"
        "openRawStream('new');\n"
        "const newer = opened[1];\n"
        "const model = state.raw;\n"
        "const rendersBeforeStale = renders;\n"
        "older.fire('done', {data: JSON.stringify({outcome: 'read'})});\n"
        "const afterDone = {open: !newer.closed && state.events === newer, pane: state.raw === model};\n"
        "older.fire('error', {});\n"
        "const afterError = {\n"
        "  open: !newer.closed && state.events === newer,\n"
        "  pane: state.raw === model && model.available === true && model.finished === false,\n"
        "};\n"
        "const rendersAfterStale = renders;\n"
        "state.raw = null;\n"
        "newer.fire('error', {});\n"
        "console.log(JSON.stringify({\n"
        "  afterDone: afterDone,\n"
        "  afterError: afterError,\n"
        "  staleRenders: rendersAfterStale - rendersBeforeStale,\n"
        "  oldClosed: older.closed,\n"
        "  nullRawDidNotThrow: true,\n"
        "}));"
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    answered = json.loads(result.stdout)

    assert answered["afterDone"] == {"open": True, "pane": True}, (
        "an old job's ``done`` never closes the new job's stream or replaces its model"
    )
    assert answered["afterError"] == {"open": True, "pane": True}, (
        "an old job's connection error leaves the new job's stream open and its pane untouched"
    )
    assert answered["staleRenders"] == 0, "neither stale handler redraws the new job's pane"
    assert answered["oldClosed"] is True, "the open that replaced the old job still closed the old stream"
    assert answered["nullRawDidNotThrow"] is True, "an error while ``state.raw`` is null does not throw"


def test_the_page_opens_one_event_stream_per_job_and_closes_it_on_done() -> None:
    """One EventSource for the job, closed on ``done`` and on the next upload, so nothing leaks."""
    page = demo_page.DEMO_HTML
    assert page.count("new EventSource(") == 1, "one stream, reopened per job"

    open_stream = _function("openRawStream")
    assert open_stream.index("closeRawStream()") < open_stream.index("new EventSource(")
    assert 'source.addEventListener("done"' in open_stream
    assert "closeRawStream();" in open_stream.split('source.addEventListener("done"')[1]
    assert "closeRawStream()" in _function("upload"), "a new upload closes the stream it replaces"


def test_the_raw_pane_carries_a_copy_button_and_the_page_still_needs_no_build_step() -> None:
    """The copy button copies the stream verbatim, and the asset is still the one HTML string it was."""
    page = demo_page.DEMO_HTML
    assert '<button type="button" id="raw-copy"' in page
    assert 'byId("raw-copy").addEventListener("click", copyRaw)' in page
    copy = _function("copyRaw")
    assert "navigator.clipboard.writeText(text)" in copy
    assert "rawText(state.raw)" in copy

    assert page.count("<script>") == 1 and "<script src" not in page
    assert 'type="module"' not in page and "import " not in page
    assert "cdn." not in page and "unpkg" not in page
