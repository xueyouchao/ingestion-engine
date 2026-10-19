"""The engine adapters.

The HTTP engines are exercised against a real stub model server on localhost, so the wire format is
proven end to end. The command-line engines are exercised with a recording runner that writes the
files the tool would write, which proves the command and the parsing around it.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

import pymupdf
import pytest

from finepdfs.domain.document import Document
from finepdfs.infrastructure.engines.docling_engine import (
    DoclingEngine,
    DoclingError,
    DoclingVersionError,
    pages_from_docling_json,
)
from finepdfs.infrastructure.engines.mineru import (
    MineruEngine,
    MineruError,
    MineruHttpBackend,
    MineruSubprocessBackend,
    content_list_from_response,
    pages_from_content_list,
)
from finepdfs.infrastructure.engines.paddleocr_vl import (
    PaddleOcrError,
    PaddleOcrVlCliEngine,
    PaddleOcrVlEngine,
    page_from_paddle_result,
    pages_from_paddle_output,
)
from finepdfs.infrastructure.engines.pdf_document import PdfDocument
from finepdfs.infrastructure.engines.subprocess_support import CommandResult
from finepdfs.infrastructure.engines.vlm_chat import (
    ChatEndpoint,
    VlmChatTransport,
    VlmTransportError,
    strip_model_preamble,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class StubModelServer:
    """A model server that records requests and answers from a canned response."""

    def __init__(self, response: dict) -> None:
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                payload = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class RecordingRunner:
    """A command runner that records the command and can write the files the tool would write."""

    def __init__(self, on_run=None, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.argv: list[str] | None = None
        self._on_run = on_run
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def run(self, argv, cwd=None) -> CommandResult:
        self.argv = list(argv)
        if self._on_run is not None:
            self._on_run(list(argv))
        return CommandResult(
            argv=tuple(argv), returncode=self._returncode, stdout=self._stdout, stderr=self._stderr
        )


def _two_page_pdf() -> bytes:
    document = pymupdf.open()
    for number in (1, 2):
        page = document.new_page(width=595, height=842)
        page.insert_text((72, 120), f"synthetic page {number}")
    content = document.tobytes()
    document.close()
    return content


@pytest.fixture
def paddle_server():
    server = StubModelServer({"choices": [{"message": {"role": "assistant", "content": "OCR:"}}]})
    yield server
    server.stop()


class RenderRecord(NamedTuple):
    """One page render: the page, the image it produced, and the thread that rendered it."""

    thread: threading.Thread
    page_number: int
    image: bytes


@pytest.fixture
def renders(monkeypatch) -> list[RenderRecord]:
    """Record every render of an extraction, so a test can say where it happened.

    Rendering one page at a time through the one open PDF handle, on the thread that walks the pages,
    is a rule of ADR-0010 that no test may negotiate: PyMuPDF is not thread-safe.
    """
    record: list[RenderRecord] = []
    original = PdfDocument.render_page_png

    def recording_render(pdf: PdfDocument, page_number: int, dpi: int = 200) -> bytes:
        image = original(pdf, page_number, dpi=dpi)
        record.append(RenderRecord(threading.current_thread(), page_number, image))
        return image

    monkeypatch.setattr(PdfDocument, "render_page_png", recording_render)
    return record


class GatedTransport(VlmChatTransport):
    """A transport whose page requests can be held at a gate, and that says where each one ran.

    The page a request belongs to is read from the image the engine rendered, and the answer names
    that page, so a test never has to guess which request it is looking at.
    """

    def __init__(self, renders: list[RenderRecord], gate: Callable[[int], None]) -> None:
        super().__init__(ChatEndpoint(base_url="http://stub.invalid/v1", model="PaddleOCR-VL-1.6"))
        self._renders = renders
        self._gate = gate
        self.threads: list[threading.Thread] = []
        self.failures: dict[int, Exception] = {}

    def complete_with_page_image(self, prompt: str, image_png: bytes) -> str:
        page_number = next(page for _, page, image in list(self._renders) if image == image_png)
        self.threads.append(threading.current_thread())
        self._gate(page_number)
        failure = self.failures.get(page_number)
        if failure is not None:
            raise failure
        return f"page {page_number} text"


class CountingTransport(GatedTransport):
    """A gated transport that also records how many page requests were in flight at once."""

    def __init__(self, renders: list[RenderRecord], gate: Callable[[int], None]) -> None:
        super().__init__(renders, gate)
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()

    def complete_with_page_image(self, prompt: str, image_png: bytes) -> str:
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            return super().complete_with_page_image(prompt, image_png)
        finally:
            with self._lock:
                self.running -= 1


def test_every_document_shares_one_bound_on_how_many_page_requests_are_in_flight(renders) -> None:
    """The bound is the run's, not a Document's: two Documents never put more than the knob's requests
    in flight between them. A pool built inside one read would let a run reach Documents x limit.
    """
    # Every page of both Documents, all at once, is what a bound per Document would put in flight: the
    # two that the run's bound allows are never joined by the other two, and wait their turn instead.
    all_four = threading.Barrier(4, timeout=0.25)

    def wait_for_every_request(_page_number: int) -> None:
        # Four requests meet only if the bound is per Document; under the run's bound the pair that got
        # in waits out the rendezvous and goes on serially.
        with contextlib.suppress(threading.BrokenBarrierError):
            all_four.wait()

    transport = CountingTransport(renders, gate=wait_for_every_request)
    engine = PaddleOcrVlEngine(transport=transport, dpi=72, concurrency=2)
    content = _two_page_pdf()
    start = threading.Barrier(3)
    extracted: list = []
    failures: list[BaseException] = []

    def read(name: str) -> None:
        start.wait()
        try:
            extracted.append(engine.extract(Document(name=name, content=content)))
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=read, args=(f"https://a.example/{number}.pdf",)) for number in (1, 2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads), "the run stalled: a read never came back"
    assert failures == []
    assert len(extracted) == 2, "both Documents were read"
    assert transport.peak == 2, f"{transport.peak} page requests were in flight at once, and the knob said two"
    for extracted_document in extracted:
        assert [(page.number, page.text) for page in extracted_document.pages] == [(1, "page 1 text"), (2, "page 2 text")]


def test_paddleocr_vl_keeps_a_page_request_in_flight_while_the_next_page_renders(renders) -> None:
    """Two page requests are in flight at once, so neither answers until the other has arrived.

    ADR-0010 step 2: page *k*'s request overlaps page *k+1*'s render. A serial engine walks into the
    gate alone and fails after its timeout instead of hanging the suite.
    """
    arrivals = threading.Barrier(2, timeout=5)

    def wait_for_the_second_request(_page_number: int) -> None:
        try:
            arrivals.wait()
        except threading.BrokenBarrierError:
            raise AssertionError(
                "the second page request had not arrived within 5 s: this engine sends its page "
                "requests one after another instead of overlapping them (ADR-0010 step 2)"
            ) from None

    transport = GatedTransport(renders, gate=wait_for_the_second_request)
    engine = PaddleOcrVlEngine(transport=transport, dpi=72, concurrency=2)

    extracted = engine.extract(Document(name="https://a.example/two.pdf", content=_two_page_pdf()))

    assert [(page.number, page.text) for page in extracted.pages] == [(1, "page 1 text"), (2, "page 2 text")]


def test_paddleocr_vl_keeps_page_texts_in_page_number_order_when_later_pages_answer_first(renders) -> None:
    """A Page Text carries the number of the page that produced it, never the slot it arrived in.

    Page 2's answer arrives first and page 1's waits behind it, so reassembly by completion order
    would hand back page 2's text under page 1's number.
    """
    page_two_answered = threading.Event()

    def answer_page_two_first(page_number: int) -> None:
        if page_number == 2:
            page_two_answered.set()
        elif not page_two_answered.wait(timeout=5):
            raise AssertionError(
                "page 2's request never ran while page 1's was in flight: the page requests are serial"
            )

    transport = GatedTransport(renders, gate=answer_page_two_first)
    engine = PaddleOcrVlEngine(transport=transport, dpi=72, concurrency=2)

    extracted = engine.extract(Document(name="https://a.example/two.pdf", content=_two_page_pdf()))

    assert [(page.number, page.text) for page in extracted.pages] == [(1, "page 1 text"), (2, "page 2 text")]


def test_paddleocr_vl_renders_every_page_on_the_extracting_thread(renders) -> None:
    """Rendering stays serial, on the thread that walks the pages, while the requests go to the pool.

    Moving a render into the pool is the one change ADR-0010 step 2 must never make: PyMuPDF is not
    thread-safe and every page is rendered through the one open PDF handle, so two threads on it can
    take the whole run down. This test is the guard on that rule.
    """
    transport = GatedTransport(renders, gate=lambda _page_number: None)
    engine = PaddleOcrVlEngine(transport=transport, dpi=72, concurrency=2)
    extracting_thread = threading.current_thread()

    engine.extract(Document(name="https://a.example/two.pdf", content=_two_page_pdf()))

    assert [record.page_number for record in renders] == [1, 2], "every page is rendered, once"
    assert {record.thread for record in renders} == {extracting_thread}, (
        "a page was rendered off the extracting thread, so two threads were on one PDF handle"
    )
    assert len(transport.threads) == 2
    assert extracting_thread not in transport.threads, "the page requests did not leave the extracting thread"


def test_a_page_that_fails_fails_its_document_and_frees_its_slot_on_the_run_s_bound(renders) -> None:
    """A page that fails fails its Document (ADR-0010 Consequences) and does not cost the run a slot.

    The pool belongs to the Engine and now outlives a failed read, so what a failure must not do is keep
    what it held: no request is left running, and the next Document is read with the bound unchanged.
    """
    transport = CountingTransport(renders, gate=lambda _page_number: None)
    transport.failures[2] = VlmTransportError("the model server answered 500")
    engine = PaddleOcrVlEngine(transport=transport, dpi=72, concurrency=1)

    with pytest.raises(VlmTransportError, match="500"):
        engine.extract(Document(name="https://a.example/two.pdf", content=_two_page_pdf()))

    assert transport.running == 0, "the failed read left a page request running"

    transport.failures.clear()
    extracted = engine.extract(Document(name="https://a.example/two.pdf", content=_two_page_pdf()))

    assert [page.number for page in extracted.pages] == [1, 2]
    assert transport.peak == 1, f"{transport.peak} requests were in flight after the failure; the bound was one"


def test_a_concurrency_of_one_still_reads_every_page_in_page_number_order(renders) -> None:
    """The bound is a knob, and its lowest setting is a serial read, not a broken one."""
    transport = GatedTransport(renders, gate=lambda _page_number: None)
    engine = PaddleOcrVlEngine(transport=transport, dpi=72, concurrency=1)

    extracted = engine.extract(Document(name="https://a.example/two.pdf", content=_two_page_pdf()))

    assert [(page.number, page.text) for page in extracted.pages] == [(1, "page 1 text"), (2, "page 2 text")]


def test_how_many_page_requests_overlap_is_not_part_of_the_engine_identity(renders) -> None:
    """The memo key remembers the Engine and its page DPI, never how many requests it sends at once.

    ADR-0005 keys an Extraction on ``model@base_url@dpi<dpi>``; making the requests overlap must not
    move that key, or every Document the engine already read would be read again.
    """
    transport = GatedTransport(renders, gate=lambda _page_number: None)

    serial = PaddleOcrVlEngine(transport=transport, dpi=200, concurrency=1)
    overlapped = PaddleOcrVlEngine(transport=transport, dpi=200, concurrency=8)

    assert overlapped.identity.detail == "PaddleOCR-VL-1.6@http://stub.invalid/v1@dpi200"
    assert overlapped.identity == serial.identity


def test_paddleocr_vl_reads_every_page_through_a_chat_endpoint(paddle_server) -> None:
    server = StubModelServer(
        {"choices": [{"message": {"role": "assistant", "content": "## Extracted heading\n\nbody text"}}]}
    )
    engine = PaddleOcrVlEngine(
        transport=VlmChatTransport(ChatEndpoint(base_url=server.base_url + "/v1", model="PaddleOCR-VL-1.6")),
        dpi=72,
    )
    try:
        extracted = engine.extract(Document(name="https://a.example/two.pdf", content=_two_page_pdf()))
    finally:
        server.stop()

    assert [page.number for page in extracted.pages] == [1, 2]
    assert all("body text" in page.text for page in extracted.pages)
    assert extracted.engine.name == "paddleocr-vl"
    assert "PaddleOCR-VL-1.6" in str(extracted.engine)

    assert len(server.requests) == 2
    assert server.requests[0]["path"] == "/v1/chat/completions"
    body = json.loads(server.requests[0]["body"])
    assert body["model"] == "PaddleOCR-VL-1.6"
    assert body["temperature"] == 0
    content = body["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["text"] == "OCR:"


def test_paddleocr_vl_reports_a_server_that_answers_without_content() -> None:
    server = StubModelServer({"error": "the model is not loaded"})
    engine = PaddleOcrVlEngine(
        transport=VlmChatTransport(ChatEndpoint(base_url=server.base_url + "/v1", model="m")),
        dpi=72,
    )
    try:
        with pytest.raises(VlmTransportError) as raised:
            engine.extract(Document(name="x", content=_two_page_pdf()))
    finally:
        server.stop()

    assert "without message content" in str(raised.value)


def test_the_paddleocr_result_parser_reads_blocks_in_reading_order() -> None:
    payload = json.loads((FIXTURES / "paddle_res_page.json").read_text())

    page = page_from_paddle_result(payload)

    assert page.number == 1
    assert page.text.startswith("WMO Resolution 16")
    assert "The Congress noted the damage." in page.text
    assert page.text.index("WMO Resolution 16") < page.text.index("The Congress")


def test_the_paddleocr_result_parser_refuses_an_unrelated_document() -> None:
    with pytest.raises(PaddleOcrError):
        page_from_paddle_result({"res": {"page_index": 0}})


def test_the_paddleocr_command_line_engine_reads_one_result_per_rendered_page(tmp_path) -> None:
    def write_results(argv: list[str]) -> None:
        output_dir = Path(argv[argv.index("--save_path") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        for index in (1, 2):
            (output_dir / f"page-{index:04d}_res.json").write_text(
                json.dumps({"res": {"page_index": index - 1, "parsing_res_list": [{"block_content": f"page {index} text", "block_order": 0}]}})
            )

    runner = RecordingRunner(on_run=write_results)
    engine = PaddleOcrVlCliEngine(work_dir=tmp_path, runner=runner)

    extracted = engine.extract(Document(name="x", content=_two_page_pdf()))

    assert [page.text for page in extracted.pages] == ["page 1 text", "page 2 text"]
    assert runner.argv[:2] == ["paddleocr", "doc_parser"]
    assert "--pipeline_version" in runner.argv and "v1.6" in runner.argv
    assert extracted.engine.detail == "cli:paddleocr:v1.6@dpi200"


def test_the_paddleocr_command_line_engine_fails_loudly_when_nothing_was_written(tmp_path) -> None:
    engine = PaddleOcrVlCliEngine(work_dir=tmp_path, runner=RecordingRunner())

    with pytest.raises(PaddleOcrError):
        engine.extract(Document(name="x", content=_two_page_pdf()))


def test_pages_from_paddle_output_sorts_pages_by_file_name(tmp_path) -> None:
    (tmp_path / "page-0002_res.json").write_text(json.dumps({"res": {"page_index": 1, "parsing_res_list": [{"block_content": "second"}]}}))
    (tmp_path / "page-0001_res.json").write_text(json.dumps({"res": {"page_index": 0, "parsing_res_list": [{"block_content": "first"}]}}))

    pages = pages_from_paddle_output(tmp_path, page_count=2)

    assert [page.text for page in pages] == ["first", "second"]


def test_the_mineru_content_list_parser_groups_blocks_into_pages() -> None:
    items = json.loads((FIXTURES / "mineru_content_list.json").read_text())

    pages = pages_from_content_list(items)

    assert [page.number for page in pages] == [1, 2]
    assert pages[0].text.startswith("WMO Resolution 16")
    assert "<table>" in pages[0].text
    assert pages[1].text == "E = mc^2\n\nFigure 1: a damaged anemometer\n\nThe Congress resolved to repair the station."


def test_mineru_reads_a_service_that_answers_with_a_content_list() -> None:
    items = json.loads((FIXTURES / "mineru_content_list.json").read_text())
    server = StubModelServer({"results": {"document.pdf": {"md_content": "ignored", "content_list": items}}})
    engine = MineruEngine(backend=MineruHttpBackend(base_url=server.base_url))
    try:
        extracted = engine.extract(Document(name="document.pdf", content=b"%PDF-1.7 fake"))
    finally:
        server.stop()

    assert next(page.text for page in extracted.pages).startswith("WMO Resolution 16")
    assert engine.identity.name == "mineru"
    assert "MinerU2.5-Pro" in str(engine.identity)

    request = server.requests[0]
    assert request["path"] == "/file_parse"
    assert request["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="files"; filename="document.pdf"' in request["body"]
    assert b"%PDF-1.7 fake" in request["body"]
    assert b"return_content_list" in request["body"]


def test_the_mineru_command_line_engine_reads_the_content_list_it_wrote(tmp_path) -> None:
    items = json.loads((FIXTURES / "mineru_content_list.json").read_text())

    def write_content_list(argv: list[str]) -> None:
        output_dir = Path(argv[argv.index("-o") + 1]) / "document"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "document_content_list.json").write_text(json.dumps(items))

    runner = RecordingRunner(on_run=write_content_list)
    engine = MineruEngine(backend=MineruSubprocessBackend(work_dir=tmp_path, runner=runner))

    extracted = engine.extract(Document(name="x", content=b"%PDF-1.7 fake"))

    assert extracted.page_count == 2
    assert runner.argv[0] == "mineru"
    assert runner.argv[runner.argv.index("-b") + 1] == "pipeline"


def test_the_mineru_command_line_engine_fails_loudly_when_nothing_was_written(tmp_path) -> None:
    engine = MineruEngine(backend=MineruSubprocessBackend(work_dir=tmp_path, runner=RecordingRunner()))

    with pytest.raises(MineruError):
        engine.extract(Document(name="x", content=b"%PDF"))


def test_content_list_from_response_refuses_an_answer_without_one() -> None:
    with pytest.raises(MineruError):
        content_list_from_response({"results": {"document.pdf": {"md_content": "only markdown"}}})


def test_the_docling_engine_reads_the_page_aware_json_it_asked_for(tmp_path) -> None:
    def write_json(argv: list[str]) -> None:
        if "--output" not in argv:
            return
        output_dir = Path(argv[argv.index("--output") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "document.json").write_text(
            (FIXTURES / "docling_2_140_0_cc_scan_trimmed.json").read_text()
        )

    command = str(tmp_path / "docling")
    runner = RecordingRunner(on_run=write_json, stdout="Docling version: 2.140.0\n")
    engine = DoclingEngine(command=command, work_dir=tmp_path, runner=runner)

    extracted = engine.extract(Document(name="x", content=b"%PDF-1.7 fake"))

    assert runner.argv[0] == command
    assert runner.argv[1] == "convert"
    assert runner.argv[2:4] == ["--to", "json"]
    assert runner.argv[runner.argv.index("--image-export-mode") + 1] == "placeholder"
    assert [page.number for page in extracted.pages] == [1, 2]
    assert extracted.pages[0].text.split("\n\n")[0] == "Flight ID"
    assert "Murilk" in extracted.pages[0].text
    assert "GPS Dropwindsonde Scientist Log (Revised 5/2002}" in extracted.pages[1].text
    assert "Sonde | Tle (JTC) | Iat" in extracted.pages[1].text
    assert "WxwxA Dannyl" not in "\n".join(page.text for page in extracted.pages)
    assert extracted.engine.name == "docling"
    assert extracted.engine.detail == "cli:docling:v2.140.0"


def test_a_docling_version_probe_that_cannot_answer_is_a_named_error(tmp_path) -> None:
    """The version is part of the memo key, so a probe with no answer must not degrade to a bare name.

    ADR-0005 keys Extraction on the Engine Identity; an identity that silently drops the tool version
    would let an upgraded Docling reuse an Extraction the old one produced.
    """
    engine = DoclingEngine(
        command=str(tmp_path / "docling"), runner=RecordingRunner(returncode=1, stderr="boom")
    )

    with pytest.raises(DoclingVersionError, match="version probe"):
        assert engine.identity


def test_repeated_identity_calls_probe_the_tool_once(tmp_path) -> None:
    probes: list[list[str]] = []
    runner = RecordingRunner(on_run=lambda argv: probes.append(argv), stdout="Docling version: 2.140.0\n")
    engine = DoclingEngine(command=str(tmp_path / "docling"), runner=runner)

    assert engine.identity.detail == "cli:docling:v2.140.0"
    assert engine.identity.detail == "cli:docling:v2.140.0"
    assert len(probes) == 1, "a tool version is a property of the installed executable"


def test_the_docling_engine_fails_loudly_when_nothing_was_written(tmp_path) -> None:
    runner = RecordingRunner(stdout="Docling version: 2.140.0\n")
    with pytest.raises(DoclingError):
        DoclingEngine(work_dir=tmp_path, runner=runner).extract(Document(name="x", content=b"%PDF"))


def test_the_docling_engine_refuses_json_it_cannot_parse(tmp_path) -> None:
    def write_broken_json(argv: list[str]) -> None:
        if "--output" not in argv:
            return
        output_dir = Path(argv[argv.index("--output") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "document.json").write_text("{not json")

    engine = DoclingEngine(
        work_dir=tmp_path,
        runner=RecordingRunner(on_run=write_broken_json, stdout="Docling version: 2.140.0\n"),
    )

    with pytest.raises(DoclingError, match="invalid JSON"):
        engine.extract(Document(name="x", content=b"%PDF"))


def test_docling_json_keeps_the_original_page_numbers_of_a_sparse_document() -> None:
    payload = {
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
        "texts": [
            {"label": "text", "text": "first", "prov": [{"page_no": 1}]},
            {"label": "text", "text": "third", "prov": [{"page_no": 3}]},
        ],
    }

    pages = pages_from_docling_json(payload)

    assert [(page.number, page.text) for page in pages] == [(1, "first"), (3, "third")]


def test_docling_json_skips_unplaceable_items_and_reads_the_fallback_text_items() -> None:
    payload = {
        "body": {"children": [{"$ref": "#/texts/9"}, "not a reference"]},
        "texts": [
            {"label": "text", "text": "has no provenance"},
            {"label": "text", "text": "second page", "prov": [{"page_no": 2}]},
        ],
    }

    pages = pages_from_docling_json(payload)

    assert [(page.number, page.text) for page in pages] == [(2, "second page")]


def test_strip_model_preamble_removes_an_echoed_prompt_and_code_fences() -> None:
    assert strip_model_preamble("OCR:\nreal text", "OCR:") == "real text"
    assert strip_model_preamble("```markdown\nreal text\n```", "OCR:") == "real text"
    assert strip_model_preamble("real text", "OCR:") == "real text"


@pytest.mark.parametrize("content", [None, 12345, {"text": "body"}, ["body", 7], [{"text": 7}], []])
def test_paddleocr_vl_refuses_answer_content_that_is_not_text(content) -> None:
    """str() around whatever arrived would turn an error or a tool call into a page saying "None"."""
    server = StubModelServer({"choices": [{"message": {"content": content}}]})
    engine = PaddleOcrVlEngine(
        transport=VlmChatTransport(ChatEndpoint(base_url=server.base_url + "/v1", model="m")),
        dpi=72,
    )
    try:
        with pytest.raises(VlmTransportError) as raised:
            engine.extract(Document(name="x", content=_two_page_pdf()))
    finally:
        server.stop()

    assert "not text" in str(raised.value)


def test_paddleocr_vl_reads_content_parts_when_the_server_sends_them() -> None:
    server = StubModelServer(
        {"choices": [{"message": {"content": [{"text": "part one "}, {"text": "part two"}]}}]}
    )
    engine = PaddleOcrVlEngine(
        transport=VlmChatTransport(ChatEndpoint(base_url=server.base_url + "/v1", model="m")),
        dpi=72,
    )
    try:
        extracted = engine.extract(Document(name="x", content=_two_page_pdf()))
    finally:
        server.stop()

    assert extracted.pages[0].text == "part one part two"


def test_the_mineru_multipart_filename_cannot_break_out_of_its_part() -> None:
    """The manifest is user-controlled, so a name with a quote or a newline must not split the body."""
    server = StubModelServer({"results": {"document.pdf": {"content_list": []}}})
    backend = MineruHttpBackend(base_url=server.base_url)
    try:
        backend.content_list(Document(name='ev"il\r\nX-Injected: 1', content=b"%PDF-1.7 fake"))
    finally:
        server.stop()

    body = server.requests[0]["body"]
    assert body.count(b'name="files"') == 1
    assert b'filename="ev_il__X-Injected: 1"' in body
