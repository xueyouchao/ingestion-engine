"""The demo's document answer over HTTP: the rendered fragment and the markdown it offers.

The document is its own answer, never a field of the job JSON (ADR-0014). These tests drive the real
``ThreadingHTTPServer`` on port 0 with the real queue and a reader that answers fixed Page Texts, and
check the two surfaces: the fragment, which is built from Page Text and is inert for hostile input, and
the ``.md``, which is exactly the shared projection of the same pages. The golden case reads a checked-in
page-record file shaped like ``out/pages/<key>.jsonl`` so the pane is proven against the corpus's bytes,
not against a screenshot.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

import pytest

import finepdfs.interfaces.demo_render as demo_render
from finepdfs.application.curate import RoutingPlan
from finepdfs.application.demo_read import FailedReading, Reading
from finepdfs.domain.document import EngineIdentity, PageText, TextLayerQuality
from finepdfs.domain.ocr import OcrRoutingPolicy, RoutingDecision
from finepdfs.domain.publishing import PageRecord, document_markdown
from finepdfs.interfaces.demo_server import DemoApp, make_server

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
GOLDEN = FIXTURES / "pages" / "ba02d52504f527bb.jsonl"
ENGINE = EngineIdentity("paddleocr-vl", "weights-c5630abae1d9")


class Document:
    """What a page's fragment looks like, parsed: page sections with their state and attributes."""

    def __init__(self, fragment: str) -> None:
        self.fragment = fragment
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.sections: list[tuple[str | None, str | None, str]] = []
        self.text = ""
        self._collect(fragment)

    def _collect(self, fragment: str) -> None:
        outer = self

        class Parser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.section: tuple[str | None, str | None, list[str]] | None = None
                self.data: list[str] = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                outer.elements.append((tag, attrs))
                if tag == "section":
                    self.section = (attrs.get("data-page"), attrs.get("data-state"), [])

            def handle_endtag(self, tag):
                if tag == "section" and self.section is not None:
                    outer.sections.append((self.section[0], self.section[1], "".join(self.section[2])))
                    self.section = None

            def handle_data(self, data):
                self.data.append(data)
                if self.section is not None:
                    self.section[2].append(data)

        parser = Parser()
        parser.feed(fragment)
        self.text = "".join(parser.data)

    def section(self, number: str) -> tuple[str | None, str | None, str]:
        for section in self.sections:
            if section[0] == number:
                return section
        raise AssertionError(f"no page {number} in {self.fragment!r}")


def _words(text: str) -> list[str]:
    return text.split()


def _reading(pages: tuple[PageText, ...]) -> Reading:
    quality = TextLayerQuality(
        page_count=len(pages),
        pages_with_text=len(pages),
        character_count=sum(len(page.text) for page in pages),
        garbled_ratio=0.0,
        image_dominated_pages=0,
    )
    return Reading(
        plan=RoutingPlan(
            quality=quality,
            decision=RoutingDecision(False, "the text layer is healthy", {"page_count": float(len(pages))}),
            engine_name="pymupdf",
        ),
        thresholds=OcrRoutingPolicy(),
        engine=ENGINE,
        pages=pages,
        elapsed_ms=1,
        warm_ms=None,
    )


def _reads(pages: tuple[PageText, ...]) -> Callable:
    def read(content, client, on_stage, name):
        on_stage("routing")
        on_stage("reading")
        on_stage("done")
        return _reading(pages)

    return read


class BlockingReader:
    """A reader held open until the test releases it, so a job can be seen mid-read."""

    def __init__(self, outcome: Reading | FailedReading) -> None:
        self._outcome = outcome
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, content, client, on_stage, name):
        on_stage("routing")
        on_stage("reading")
        self.started.set()
        assert self.release.wait(5), "the test never released the reading"
        on_stage("done")
        return self._outcome


@contextmanager
def _serving(app: DemoApp):
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        app.close()


def _post(base: str, body: bytes = b"%PDF-1.7 uploaded") -> str:
    request = urllib.request.Request(
        base + "/api/read", data=body, method="POST", headers={"Content-Type": "application/pdf"}
    )
    with urllib.request.urlopen(request) as response:
        assert response.status == 202
        return json.loads(response.read())["job"]


def _get(url: str):
    return urllib.request.urlopen(url)


def _wait_for_done(base: str, job_id: str) -> dict:
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{base}/api/read/{job_id}") as response:
            answer = json.loads(response.read())
        if answer["stage"] == "done":
            return answer
        time.sleep(0.01)
    raise AssertionError("the demo never finished the reading")


def _app(read: Callable) -> DemoApp:
    return DemoApp(limits=None, engine_name="paddleocr-vl", environ={}, read=read)


def _golden_records() -> list[PageRecord]:
    rows = [line for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]
    # PageRecord(**row) is the shape test: this fixture is exactly what as_json() writes, no extra field
    # and no missing one, so a change to the page-record contract cannot leave this document behind.
    records = [PageRecord(**json.loads(row)) for row in rows]
    assert [record.page_number for record in records] == [1, 2, 3]
    assert {record.engine for record in records} == {str(ENGINE)}
    assert {record.doc_id for record in records} == {"ba02d52504f527bb"}
    return records


def test_the_document_carries_the_page_records_bytes_in_page_order_with_page_separators() -> None:
    records = _golden_records()
    # Arrival order is deliberately not page order: the pane sorts by the original 1-based number.
    pages = tuple(PageText(number=record.page_number, text=record.text) for record in reversed(records))
    app = _app(_reads(pages))
    with _serving(app) as base:
        job_id = _post(base)
        _wait_for_done(base, job_id)

        with _get(f"{base}/api/read/{job_id}/document") as response:
            html = response.read().decode("utf-8")
            content_type = response.headers["Content-Type"]
            headers = dict(response.headers)
        with _get(f"{base}/api/read/{job_id}/document.md") as response:
            markdown = response.read().decode("utf-8")
            markdown_type = response.headers["Content-Type"]

    document = Document(html)
    assert content_type.startswith("text/html")
    assert headers["X-Document-Pages"] == "3"
    assert headers["X-Document-Completed"] == "3"
    assert [section[0] for section in document.sections] == ["1", "2", "3"]
    assert {section[1] for section in document.sections} == {"complete"}
    for number, page in enumerate(records, start=1):
        _, _, section_text = document.section(str(number))
        assert f'<h2 class="document-page-number">Page {number}</h2>' in html
        # The section holds the pane's separator ("Page N") and then exactly the record's Page Text.
        carried = section_text.replace(f"Page {number}", "", 1)
        assert _words(carried) == _words(page.text), "the page's own bytes, in its own place"

    assert markdown_type.startswith("text/markdown")
    projection = document_markdown(tuple(PageText(record.page_number, record.text) for record in records))
    assert markdown == projection, "the .md is the corpus's projection, byte for byte"


def test_a_page_still_being_read_is_only_a_skeleton_and_a_delta_never_reaches_the_pane() -> None:
    outcome = _reading((PageText(1, "the first completed page"), PageText(2, "the second completed page")))
    reader = BlockingReader(outcome)
    app = _app(reader)
    with _serving(app) as base:
        job_id = _post(base)
        assert reader.started.wait(5)
        app.documents.begin(job_id, page_count=2)
        app.documents.page(job_id, PageText(1, "the first completed page"))
        sink = app.documents.sink(job_id)
        sink.on_delta(2, "| a half | parsed table |")  # a delta is the transcript pane's, not this one

        with _get(f"{base}/api/read/{job_id}/document") as response:
            html = response.read().decode("utf-8")
        with _get(f"{base}/api/read/{job_id}/document.md") as response:
            markdown = response.read().decode("utf-8")

        reader.release.set()
        _wait_for_done(base, job_id)

    document = Document(html)
    assert document.section("1")[1] == "complete"
    assert document.section("2")[1] == "pending"
    assert '<p class="skeleton" data-state="pending">Page 2 is still being read.</p>' in html
    assert "half" not in document.text, "a half-parsed table is never rendered"
    assert "second completed page" not in document.text, "a page appears only when its Page Text exists"
    assert markdown == "the first completed page", "the download carries the pages that are complete"


def test_a_failed_read_keeps_the_pages_that_already_completed() -> None:
    failure = FailedReading(
        kind="failed",
        lane="ocr",
        reason="the worker broke its stream off",
        plan=_reading((PageText(1, "first"),)).plan,
        thresholds=OcrRoutingPolicy(),
    )
    reader = BlockingReader(failure)
    app = _app(reader)
    with _serving(app) as base:
        job_id = _post(base)
        assert reader.started.wait(5)
        app.documents.begin(job_id, page_count=2)
        app.documents.page(job_id, PageText(1, "the page that completed before the failure"))
        with _get(f"{base}/api/read/{job_id}/document") as response:
            html = response.read().decode("utf-8")
        reader.release.set()
        answer = _wait_for_done(base, job_id)

    assert answer["outcome"] == "failed"
    assert answer["failure"]["reason"] == "the worker broke its stream off"
    document = Document(html)
    assert document.section("1")[1] == "complete"
    assert "the page that completed before the failure" in document.section("1")[2]


def test_a_hostile_page_renders_inert_and_the_job_json_still_carries_text_not_html() -> None:
    hostile = (
        "# A page the model wrote\n\n"
        "<script>alert('script')</script>\n\n"
        '<img src="x" onerror="alert(\'onerror\')">\n\n'
        "[a link](https://evil.example/steal)\n\n"
        "[click](javascript:alert('target'))\n"
    )
    app = _app(_reads((PageText(1, hostile),)))
    with _serving(app) as base:
        job_id = _post(base)
        answer = _wait_for_done(base, job_id)
        with _get(f"{base}/api/read/{job_id}") as response:
            raw_job = response.read().decode("utf-8")
        with _get(f"{base}/api/read/{job_id}/document") as response:
            fragment = response.read().decode("utf-8")

    assert answer["reading"]["pages"] == [{"number": 1, "text": hostile}]
    assert "<section" not in raw_job, "the job answer never carries the rendered document"
    assert "data-page" not in raw_job
    assert "document-page" not in raw_job

    document = Document(fragment)
    assert {tag for tag, _ in document.elements} <= {
        "article", "section", "h1", "h2", "h3", "p", "em", "strong", "code", "pre", "blockquote",
        "hr", "table", "thead", "tbody", "tr", "th", "td", "ul", "ol", "li", "br",
    }
    for tag, attrs in document.elements:
        assert tag not in {"script", "style", "iframe", "img", "a", "object", "embed", "form", "svg"}
        for name, value in attrs.items():
            assert not name.lower().startswith("on"), f"{tag}[{name}] is an event handler"
            assert name.lower() not in {"href", "src", "srcdoc", "action"}
            assert "javascript:" not in (value or "").lower()
    assert "<script" not in fragment.lower()
    assert "<img" not in fragment.lower()
    assert "<a " not in fragment.lower()
    assert "evil.example" not in fragment, "a link's target does not survive its element"
    assert "a link" in document.text and "click" in document.text, "the text stays; navigation does not"


def test_marker_residue_renders_as_the_residue_and_the_markdown_keeps_it() -> None:
    residue = "Before <|det|>table [10,20,30,40]<|/det|> after.\n\nThe row that follows."
    app = _app(_reads((PageText(1, residue),)))
    with _serving(app) as base:
        job_id = _post(base)
        _wait_for_done(base, job_id)
        with _get(f"{base}/api/read/{job_id}/document") as response:
            fragment = response.read().decode("utf-8")
        with _get(f"{base}/api/read/{job_id}/document.md") as response:
            markdown = response.read().decode("utf-8")

    assert "<|det|>table [10,20,30,40]<|/det|>" in Document(fragment).text
    assert markdown == residue, "the demo contributes no second cleanup"


def test_the_document_endpoint_says_rendering_is_not_installed(monkeypatch) -> None:
    def missing():
        raise ImportError("No module named 'nh3'")

    monkeypatch.setattr(demo_render, "_dependencies", missing)
    app = _app(_reads((PageText(1, "a page nobody may see unescaped"),)))
    with _serving(app) as base:
        job_id = _post(base)
        _wait_for_done(base, job_id)
        with pytest.raises(urllib.error.HTTPError) as caught:
            _get(f"{base}/api/read/{job_id}/document")
        with _get(f"{base}/api/read/{job_id}/document.md") as response:
            markdown = response.read().decode("utf-8")

    assert caught.value.code == 503
    assert caught.value.headers["Content-Type"].startswith("application/json")
    answer = json.loads(caught.value.read())
    assert answer["rendering"] == "not-installed"
    assert "rendering is not installed" in answer["error"]
    assert "a page nobody may see unescaped" not in answer["error"], "absence never leaks the text"
    assert markdown == "a page nobody may see unescaped", "markdown needs no optional dependency"


def test_the_markdown_answer_is_the_one_shared_projection(monkeypatch) -> None:
    """The demo must call the corpus's projection, not restate its ordering or its blank-line join."""
    import finepdfs.interfaces.demo_server as demo_server

    calls: list[tuple[int, ...]] = []

    def projection(pages) -> str:
        calls.append(tuple(page.number for page in pages))
        return "the shared projection's answer"

    monkeypatch.setattr(demo_server, "document_markdown", projection)
    app = _app(_reads((PageText(2, "second"), PageText(1, "first"))))
    with _serving(app) as base:
        job_id = _post(base)
        _wait_for_done(base, job_id)
        with _get(f"{base}/api/read/{job_id}/document.md") as response:
            markdown = response.read().decode("utf-8")

    assert markdown == "the shared projection's answer"
    assert calls == [(1, 2)], "the demo hands the shared projection the pages in page-number order"


def test_an_unknown_job_has_no_document() -> None:
    app = _app(_reads((PageText(1, "text"),)))
    with _serving(app) as base:
        for suffix in ("document", "document.md"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                _get(f"{base}/api/read/0123456789abcdef/{suffix}")
            assert caught.value.code == 404