"""The engine adapters.

The HTTP engines are exercised against a real stub model server on localhost, so the wire format is
proven end to end. The command-line engines are exercised with a recording runner that writes the
files the tool would write, which proves the command and the parsing around it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf
import pytest

from finepdfs.domain.document import Document
from finepdfs.infrastructure.engines.docling_engine import DoclingEngine, DoclingError, pages_from_markdown
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

    def __init__(self, on_run=None, returncode: int = 0, stderr: str = "") -> None:
        self.argv: list[str] | None = None
        self._on_run = on_run
        self._returncode = returncode
        self._stderr = stderr

    def run(self, argv, cwd=None) -> CommandResult:
        self.argv = list(argv)
        if self._on_run is not None:
            self._on_run(list(argv))
        return CommandResult(argv=tuple(argv), returncode=self._returncode, stdout="", stderr=self._stderr)


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


def test_the_docling_engine_runs_the_command_and_reads_the_markdown(tmp_path) -> None:
    def write_markdown(argv: list[str]) -> None:
        output_dir = Path(argv[argv.index("--output") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "document.md").write_text("# Title\n\nbody text")

    runner = RecordingRunner(on_run=write_markdown)
    engine = DoclingEngine(work_dir=tmp_path, runner=runner)

    extracted = engine.extract(Document(name="x", content=b"%PDF-1.7 fake"))

    assert runner.argv[0] == "docling"
    assert runner.argv[1:3] == ["--to", "md"]
    assert extracted.engine.name == "docling"
    assert extracted.pages[0].text == "# Title\n\nbody text"


def test_the_docling_engine_fails_loudly_when_nothing_was_written(tmp_path) -> None:
    with pytest.raises(DoclingError):
        DoclingEngine(work_dir=tmp_path, runner=RecordingRunner()).extract(Document(name="x", content=b"%PDF"))


def test_docling_page_markers_are_honoured_when_the_tool_writes_them() -> None:
    pages = pages_from_markdown("<!-- page: 1 -->\nfirst\n<!-- page: 2 -->\nsecond")

    assert [page.text for page in pages] == ["first", "second"]


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
