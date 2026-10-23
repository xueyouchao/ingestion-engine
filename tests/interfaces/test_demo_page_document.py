"""The live demo's right pane: three panes, and the rendered document inserted in one place.

ADR-0014 makes the page a row of three panes -- what was sent, what came back, what would be published
-- and the right one the server's own answer: a sanitized HTML fragment from
``GET /api/read/<id>/document``, inserted at exactly one call site. There is no browser here, so these
tests pin the asset's source (pane order, the one insertion, no second place that builds HTML) and run
the page's own pure functions under Node against fragments and answers the real HTTP server produced.
The fragment is never parsed by this page: what would be published is exactly what the server wrote.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Callable

import pytest

import finepdfs.interfaces.demo_render as demo_render
from finepdfs.application.demo_read import FailedReading
from finepdfs.domain.document import PageText
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.interfaces import demo_page, demo_server
from tests.fake_streaming_worker import FakeStreamingWorker, recorded_frames
from tests.interfaces.test_demo_page_raw import (
    _json,
    _ocr_app,
    _post,
    _scanned_pdf,
    _serving,
    _wait_for_done,
    _wait_until,
)

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node runs the page's pure script functions")

PANE_IDS = ("input-pane", "raw", "document-pane")


def _function(name: str) -> str:
    """The page's source for one top-level function, so Node can run exactly what the browser does."""
    found = re.search(rf"((?:async )?function {name}\(.*?\n\}})", demo_page.DEMO_HTML, re.S)
    assert found is not None, f"the page asset defines {name}()"
    return found.group(1)


def _run(functions: tuple[str, ...], body: str):
    """Run one snippet of the page's own JS and read its JSON answer."""
    source = "\n".join(_function(name) for name in functions)
    result = subprocess.run([NODE, "-e", f"{source}\n{body}\n"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _css() -> str:
    return demo_page.DEMO_HTML.split("<style>", 1)[1].split("</style>", 1)[0]


def test_the_page_lays_out_three_labelled_panes_in_the_reading_order() -> None:
    """Input, raw, rendered -- side by side on a wide screen, stacked in that order on a narrow one."""
    page = demo_page.DEMO_HTML

    positions = [page.index(f'<section id="{pane}"') for pane in PANE_IDS]
    assert positions == sorted(positions), "the panes appear as what was sent, what came back, what is published"
    assert page.index('<div class="panes">') < positions[0], "the three panes are the grid's children"
    assert positions[2] < page.index('<section id="decision"'), "the panes are one row before the decision"

    for pane, heading, label in (
        ("input-pane", "input-heading", "What you sent"),
        ("raw", "raw-heading", "What came back"),
        ("document-pane", "document-heading", "What would be published"),
    ):
        start = page.index(f'<section id="{pane}"')
        body = page[start : page.index("</section>", start)]
        assert f'aria-labelledby="{heading}"' in page[start : page.index(">", start) + 1]
        assert f'<h2 id="{heading}">' in body, f"the {pane} pane labels itself in words"
        assert label in body, f"the {pane} pane says which one it is"

    css = _css()
    stacked = css.index("grid-template-columns: 1fr;")
    wide = css.index("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)")
    assert stacked < wide, "the page starts stacked and only a wide enough screen puts the panes in a row"
    assert css.count("@media (min-width:") >= 1, "the row is a breakpoint, not the default"
    assert 'id="pages"' not in page, "the single-column page-text section is replaced by the rendered pane"
    assert 'id="page-texts"' not in page


def test_the_document_fragment_is_the_only_html_the_page_inserts() -> None:
    """One assignment of HTML in the whole asset, and it inserts the server's answer verbatim."""
    page = demo_page.DEMO_HTML
    assert page.count("innerHTML") == 1, "the rendered pane is the one place a fragment is inserted"
    assert "container.innerHTML = fragment;" in _function("insertDocument")
    assert 'insertDocument(byId("document"), fragment)' in _function("refreshDocument")
    assert "innerHTML" not in _function("refreshDocument"), "the call site inserts, it does not build"
    for forbidden in ("outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in page
    for render in ("renderRaw", "renderReading", "renderFailure", "renderRefusal"):
        assert "innerHTML" not in _function(render), f"{render} writes text, never markup"
    assert "textContent" in _function("renderRaw")


@requires_node
def test_the_page_inserts_exactly_the_fragment_the_server_answered() -> None:
    """Node gets a stand-in container: what the browser would insert is the server's response, byte for byte."""
    fragment = (
        '<article class="document" data-pages="2" data-completed="1">\n'
        '<section class="document-page" data-page="1" data-state="complete">'
        '<h2 class="document-page-number">Page 1</h2>\n<h1>Report</h1>\n</section>\n'
        '<section class="document-page" data-page="2" data-state="pending">'
        '<h2 class="document-page-number">Page 2</h2>\n'
        '<p class="skeleton" data-state="pending">Page 2 is still being read.</p>\n</section>\n</article>'
    )
    answer = _run(
        ("insertDocument",),
        "const container = { innerHTML: '' };\n"
        f"insertDocument(container, {json.dumps(fragment)});\n"
        "console.log(JSON.stringify({html: container.innerHTML}));",
    )
    assert answer["html"] == fragment


def test_the_pane_orders_nothing_and_has_no_second_status_vocabulary() -> None:
    """Page numbers, order and skeletons come with the fragment; the page only shows and names it."""
    page = demo_page.DEMO_HTML
    refresh = _function("refreshDocument")
    assert "document-page" not in refresh, "the page invents no sections of its own"
    assert "data-state" not in refresh, "pending/complete is the server's vocabulary, not a second one"
    assert "X-Document-Completed" in refresh and "X-Document-Pages" in refresh, "the pane reads the counts"
    assert "documentNote(" in refresh, "what has completed is said in words beside it"
    assert '"/api/read/" + encodeURIComponent(jobId) + "/document"' in refresh
    assert 'cache: "no-store"' in refresh
    assert "setInterval" not in page, "the pane rides the job poll's cadence, not a second clock"
    assert "refreshDocument(jobId)" in _function("renderJob"), "each job tick asks the document again"


@requires_node
def test_the_document_pane_is_shown_even_when_its_first_answer_cannot_be_read() -> None:
    """A fetch that fails writes its sentence into a pane the visitor can see, not into a hidden one."""
    stubs = """
const shown = new Map([["document-pane", new Set(["hidden"])]]);
function element(id) {
  const classes = shown.get(id) || new Set();
  shown.set(id, classes);
  return {
    classList: {
      remove: (name) => classes.delete(name),
      add: (name) => classes.add(name),
      contains: (name) => classes.has(name),
    },
    textContent: "",
    replaceChildren() {},
    disabled: false,
  };
}
const elements = {};
for (const id of ["document-pane", "document-note", "document", "document-copy", "document-download"]) {
  elements[id] = element(id);
}
globalThis.byId = (id) => elements[id];
globalThis.fetch = async () => { throw new Error("connection reset"); };
const state = {
  document: { job: "job-1", completed: 0, total: null, finished: false, failed: false, absent: false },
  fileName: "",
};
"""
    source = "\n".join(
        _function(name) for name in ("setDocumentNote", "setDocumentControls", "refreshDocument")
    )
    script = (
        f"{stubs}\n{source}\n"
        "refreshDocument('job-1').then(() => {\n"
        "  console.log(JSON.stringify({\n"
        "    visible: !elements['document-pane'].classList.contains('hidden'),\n"
        "    note: elements['document-note'].textContent,\n"
        "  }));\n"
        "});"
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    answered = json.loads(result.stdout)

    assert answered["visible"] is True, "the pane is shown before its answer, so its failure is readable"
    assert "could not be read" in answered["note"], "the failure is said in the pane's own note"


# -- the real server, end to end --------------------------------------------------------------------
#
# The page's three panes are fed by three real answers: the upload's job id, the job poll and the two
# document endpoints. These tests drive the same ThreadingHTTPServer on port 0 the page talks to, then
# run the page's own insertion and naming functions under Node over exactly those bytes. The streaming
# OCR composition is ticket 36's, imported rather than rebuilt, so the read is one real read.

JOB_KEYS = {
    "job",
    "stage",
    "outcome",
    "elapsed_ms",
    "warmed_ms",
    "refusal",
    "failure",
    "reading",
}


def _reading(pages: tuple[PageText, ...]):
    from finepdfs.application.curate import RoutingPlan
    from finepdfs.application.demo_read import Reading
    from finepdfs.domain.document import EngineIdentity, TextLayerQuality
    from finepdfs.domain.ocr import RoutingDecision

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
        engine=EngineIdentity("pymupdf", "fake"),
        pages=pages,
        elapsed_ms=1,
        warm_ms=None,
    )


class _PageThenBlock:
    """An OCR-shaped reader: the page count at once, page 1 when it completes, then the rest behind a gate."""

    def __init__(self, pages: tuple[PageText, ...]) -> None:
        self._pages = pages
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, content, client, on_stage, name, sink=None):
        on_stage("routing")
        on_stage("reading")
        sink.on_page_count(len(self._pages))
        sink.on_page(self._pages[0])
        self.started.set()
        assert self.release.wait(5), "the test never released the second page"
        on_stage("done")
        return _reading(self._pages)


class _FailsAfterAPage:
    """A lane that completes one page and then fails in the job's own words, mid-document."""

    def __init__(self, page: PageText, reason: str) -> None:
        self._page = page
        self._reason = reason

    def __call__(self, content, client, on_stage, name, sink=None):
        on_stage("routing")
        on_stage("reading")
        sink.on_page_count(2)
        sink.on_page(self._page)
        plan = _reading((self._page,)).plan
        return FailedReading(kind="failed", lane="ocr", reason=self._reason, plan=plan, thresholds=OcrRoutingPolicy())


def _insert(fragments: list[str]) -> list[str]:
    """Insert each fragment into one container with the page's own call site; read what the pane holds."""
    statements = "\n".join(f"insertDocument(container, {json.dumps(fragment)}); collected.push(container.innerHTML);" for fragment in fragments)
    answer = _run(
        ("insertDocument",),
        "const container = { innerHTML: '' };\nconst collected = [];\n"
        f"{statements}\nconsole.log(JSON.stringify({{collected}}));",
    )
    return answer["collected"]


def _page_numbers(fragment: str) -> list[str]:
    return re.findall(r'<section class="document-page" data-page="(\d+)"', fragment)


def _states(fragment: str) -> list[str]:
    return re.findall(r'<section class="document-page" data-page="\d+" data-state="(\w+)"', fragment)


def _fragment(url: str) -> tuple[str, dict[str, str]]:
    with urllib.request.urlopen(url) as response:
        return response.read().decode("utf-8"), dict(response.headers)


def _app(read: Callable) -> demo_server.DemoApp:
    return demo_server.DemoApp(limits=None, engine_name="paddleocr-vl", environ={}, read=read)


def test_a_skeleton_is_replaced_by_its_page_in_numeric_order_as_the_read_completes() -> None:
    """The pane is asked on the job poll's tick, so page 2 stops being a skeleton the moment it exists."""
    pages = (PageText(1, "the first completed page"), PageText(2, "the second completed page"))
    # Arrival order is deliberately reversed: the pane must show the original 1-based numbers, in order.
    reader = _PageThenBlock((pages[1], pages[0]))
    app = _app(reader)
    try:
        with _serving(app) as base:
            _, answer = _post(base, b"%PDF-1.7 uploaded")
            job = answer["job"]
            assert reader.started.wait(5)

            mid_fragment, mid_headers = _fragment(base + f"/api/read/{job}/document")
            answer = _json(base + f"/api/read/{job}")
            assert set(answer) == JOB_KEYS, "the download's name is the page's, not a new job field"

            reader.release.set()
            final = _wait_for_done(base, job)
            final_fragment, final_headers = _fragment(base + f"/api/read/{job}/document")
            markdown = urllib.request.urlopen(base + f"/api/read/{job}/document.md").read().decode("utf-8")
    finally:
        app.close()

    mid, rendered = _insert([mid_fragment, final_fragment])
    # Page 2 arrived first; the pane still shows page 1 first, as a skeleton, because the document keeps
    # the original 1-based numbers in numeric order rather than arrival order.
    assert _page_numbers(mid) == ["1", "2"], "the separator carries the original 1-based number"
    assert _states(mid) == ["pending", "complete"]
    assert '<h2 class="document-page-number">Page 2</h2>' in mid
    assert "the second completed page" in mid, "the page that completed is shown in its own place"
    assert "the first completed page" not in mid, "a page appears only when its Page Text exists"
    assert '<p class="skeleton" data-state="pending">Page 1 is still being read.</p>' in mid
    assert mid_headers["X-Document-Pages"] == "2" and mid_headers["X-Document-Completed"] == "1"

    assert _page_numbers(rendered) == ["1", "2"]
    assert _states(rendered) == ["complete", "complete"]
    assert "skeleton" not in rendered, "the skeleton was replaced by its page, not kept beside it"
    assert "the second completed page" in rendered
    assert "the first completed page" in rendered
    assert final_headers["X-Document-Completed"] == "2"
    assert final["reading"]["pages"] == [
        {"number": 1, "text": "the first completed page"},
        {"number": 2, "text": "the second completed page"},
    ]
    assert markdown == "the first completed page\n\nthe second completed page", (
        "the download is the shared projection in page order"
    )


def test_a_real_ocr_read_lands_in_the_rendered_pane_and_the_job_answer_stays_text() -> None:
    """One real streamed OCR read, one rendered page: the pane shows Page N and the worker's Page Text."""
    from tests.fake_streaming_worker import delta_text

    page_text = "".join(filter(None, (delta_text(frame) for frame in recorded_frames())))
    with FakeStreamingWorker() as worker:
        app = _ocr_app(worker)
        try:
            with _serving(app) as base:
                _, answer = _post(base, _scanned_pdf())
                job = answer["job"]
                final = _wait_for_done(base, job)
                fragment, headers = _fragment(base + f"/api/read/{job}/document")
                raw_job = urllib.request.urlopen(base + f"/api/read/{job}").read().decode("utf-8")
        finally:
            app.close()

    assert final["reading"]["lane"] == "ocr"
    assert page_text and "# Quarterly Report" in final["reading"]["pages"][0]["text"]
    assert headers["X-Document-Completed"] == headers["X-Document-Pages"] == "1"
    rendered = _insert([fragment])[0]
    assert _page_numbers(rendered) == ["1"]
    assert "Page 1" in rendered
    # The pane is the projection, not the transcript: the Page Text's own heading and table arrive as
    # markup, and the worker's fences and pipes do not survive as text.
    assert "<h1>Quarterly Report</h1>" in rendered
    assert "<table>" in rendered and "4.1 M" in rendered
    assert "Receipts were 12% higher" in rendered, "the page's own words are visible"
    assert "```markdown" not in rendered and "| Region |" not in rendered
    assert raw_job.count("<section") == 0 and "document-page" not in raw_job, "the job answer never carries HTML"


def test_a_failed_read_keeps_the_completed_pages_and_the_failure_names_the_lane() -> None:
    """A page that completed before the failure stays in the pane; the panel keeps the lane and the words."""
    reason = "the worker broke its stream off"
    reader = _FailsAfterAPage(PageText(1, "the page that completed before the failure"), reason)
    app = _app(reader)
    try:
        with _serving(app) as base:
            _, answer = _post(base, b"%PDF-1.7 uploaded")
            job = answer["job"]
            _wait_until(lambda: _json(f"{base}/api/read/{job}")["stage"] == "done")
            final = _json(f"{base}/api/read/{job}")
            fragment, headers = _fragment(base + f"/api/read/{job}/document")
    finally:
        app.close()

    assert final["outcome"] == "failed"
    assert final["failure"] == {"kind": "failed", "lane": "ocr", "reason": reason}
    assert headers["X-Document-Completed"] == "1"
    rendered = _insert([fragment])[0]
    assert _page_numbers(rendered) == ["1", "2"]
    assert _states(rendered) == ["complete", "unread"], "the page it never read is not still being read"
    assert "the page that completed before the failure" in rendered, "the pages it completed are kept"
    assert "still being read" not in rendered, "a failed read is not still reading a page"
    assert "Page 2 was not read: the read failed." in rendered

    page = demo_page.DEMO_HTML
    assert 'id="failure-lane"' in page and 'id="failure-reason"' in page
    render_job = _function("renderJob")
    refused = render_job.index('if (job.outcome === "refused")')
    final_ask = render_job.index("refreshDocument(jobId);", refused)
    non_terminal = render_job.index("refreshDocument(jobId);")
    assert non_terminal < refused < final_ask, (
        "every tick asks for the document, and only a refusal skips the final ask because it read nothing"
    )
    assert "insertDocument" not in _function("renderFailure"), "the failure panel shows the error's words only"
    assert "textContent" in _function("renderFailure")


@requires_node
def test_the_pane_note_tells_a_failed_read_from_a_document_the_pipeline_would_publish() -> None:
    """The note says what the fragment shows: pages that were not read, not a document that would be."""
    failed, read = _run(
        ("documentNote",),
        "const notes = ["
        "{job: 'a', completed: 1, total: 2, finished: true, failed: true, absent: false}, "
        "{job: 'b', completed: 2, total: 2, finished: true, failed: false, absent: false}"
        "];\nconsole.log(JSON.stringify(notes.map(documentNote)));",
    )

    assert "1 of 2 pages have completed" in failed
    assert "failed" in failed and "not read" in failed, "the pages it never reached are named as not read"
    assert "would publish" not in failed, "a failed read published nothing"
    assert "would publish" in read and "failed" not in read


@requires_node
def test_the_download_is_named_after_the_document_the_visitor_uploaded() -> None:
    """The page names the .md from the File it has; it never asks the job for a name it does not carry."""
    answer = _run(
        ("documentDownloadName",),
        "const names = ['Q3 report.pdf', 'a.scan.tiff', 'scan', '', 'notes.md'];\n"
        "console.log(JSON.stringify(names.map(documentDownloadName)));",
    )
    assert answer == ["Q3 report.md", "a.scan.md", "scan.md", "document.md", "notes.md"]

    page = demo_page.DEMO_HTML
    download = _function("downloadDocument")
    assert "documentDownloadName(state.fileName)" in download
    assert "anchor.download = name" in download, "the browser saves it under the visitor's own name"
    assert 'state.fileName = file.name || ""' in _function("upload")
    assert '"document.md"' not in download, "no fixed name when the visitor uploaded one"
    assert "documentMarkdown()" in download, "the download is the same projection the copy button copies"
    assert "navigator.clipboard.writeText(text)" in _function("copyDocument")
    assert 'byId("document-copy").addEventListener("click", copyDocument)' in page
    assert 'byId("document-download").addEventListener("click", downloadDocument)' in page


def test_the_absence_of_the_demo_extra_is_said_in_words_not_inserted_as_text(monkeypatch) -> None:
    """A 503 not-installed answer becomes this page's own sentence in the pane, never the server's bytes."""
    page_text = "a page nobody may see unescaped"

    def missing():
        raise ImportError("No module named 'nh3'")

    monkeypatch.setattr(demo_render, "_dependencies", missing)
    app = _app(lambda content, client, on_stage, name, sink=None: _reading((PageText(1, page_text),)))
    try:
        with _serving(app) as base:
            _, answer = _post(base, b"%PDF-1.7 uploaded")
            job = answer["job"]
            _wait_for_done(base, job)
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + f"/api/read/{job}/document")
            markdown = urllib.request.urlopen(base + f"/api/read/{job}/document.md").read().decode("utf-8")
    finally:
        app.close()

    assert caught.value.code == 503
    server_answer = json.loads(caught.value.read())
    assert server_answer["rendering"] == "not-installed"
    assert page_text not in server_answer["error"]

    absent = _run(("documentAbsent",), "console.log(JSON.stringify(documentAbsent()));")
    assert "Rendering is not installed" in absent
    assert "not installed" in absent and "markdown" in absent, "the pane says what is absent and what still works"
    assert page_text not in absent

    refresh = _function("refreshDocument")
    unavailable = refresh.index("response.status === 503")
    inserted = refresh.index("insertDocument(byId(")
    assert unavailable < inserted, "the absent branch answers before any fragment is inserted"
    assert "documentAbsent()" in refresh, "the pane shows its own sentence"
    assert "setDocumentNote(documentAbsent())" in refresh
    assert "insertDocument" not in refresh.split("response.status === 503")[1].split("return;")[0], (
        "absence never falls back to inserting text as HTML"
    )
    assert markdown == page_text, "the .md still answers when rendering is absent"
