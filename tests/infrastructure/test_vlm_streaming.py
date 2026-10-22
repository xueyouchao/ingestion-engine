"""The streaming page read: the opt-in wire, the verbatim report, and the page it adds up to.

ADR-0014 makes the stream opt-in and never required. A caller that does not ask keeps the blocking
answer it has today; a caller that asks gets the worker's own ``data:`` frames as they arrive. The
worker here is the shared localhost fake in ``tests/fake_streaming_worker.py``, replaying a recorded
PaddleOCR-VL page, so no network and no model are needed and the wire is the real one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from finepdfs.domain.document import Document
from finepdfs.infrastructure.engines.paddleocr_vl import (
    OCR_PROMPT,
    PaddleOcrError,
    PaddleOcrVlCliEngine,
    PaddleOcrVlEngine,
)
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextEngine
from finepdfs.infrastructure.engines.subprocess_support import CommandResult
from finepdfs.infrastructure.engines.vlm_chat import (
    ChatEndpoint,
    VlmChatTransport,
    VlmTransportError,
    strip_model_preamble,
)
from tests.fake_streaming_worker import FakeStreamingWorker, delta_text, recorded_frames

# What the recorded page says, verbatim as the worker emitted it: the prompt echoed back and the
# markdown fence the model wrapped its answer in, neither of which survives normalization.
RECORDED_ANSWER = (
    "OCR:\n"
    "```markdown\n"
    "# Quarterly Report\n"
    "\n"
    "Receipts were 12% higher than in the same quarter last year.\n"
    "\n"
    "| Region | Receipts |\n"
    "| ------ | -------- |\n"
    "| North  | 4.1 M    |\n"
    "```"
)

# The same answer cut into the frames the worker sent it in: every cut falls mid-line, so a reader
# that normalized each delta on its own would find no prompt to drop and no fence to strip.
RECORDED_DELTAS = [
    "OCR",
    ":",
    "\n``",
    "`markdown\n# Quarterly ",
    "Report\n\nReceipts were 1",
    "2% higher than in the sa",
    "me quarter last year.\n\n| Reg",
    "ion | Receipts |\n| -----",
    "- | -------- |\n| North ",
    " | 4.1 M    |\n``",
    "`",
]

# The page the same recording exports as: strip_model_preamble's own answer, kept here as a literal so
# the fixture and the expectation cannot drift together.
PAGE_TEXT = (
    "# Quarterly Report\n"
    "\n"
    "Receipts were 12% higher than in the same quarter last year.\n"
    "\n"
    "| Region | Receipts |\n"
    "| ------ | -------- |\n"
    "| North  | 4.1 M    |"
)


def _transport(worker: FakeStreamingWorker) -> VlmChatTransport:
    return VlmChatTransport(ChatEndpoint(base_url=worker.base_url, model="PaddleOCR-VL-1.6"))


class RecordingRunner:
    """A command runner that does whatever writing the tool would do, and records that it ran."""

    def __init__(self, on_run=None) -> None:
        self.argv: list[str] | None = None
        self._on_run = on_run

    def run(self, argv, cwd=None) -> CommandResult:
        self.argv = list(argv)
        if self._on_run is not None:
            self._on_run(list(argv))
        return CommandResult(argv=tuple(argv), returncode=0, stdout="", stderr="")


def _page_png() -> bytes:
    return b"\x89PNG\r\n\x1a\n a page image the fake worker never reads"


def test_a_streaming_call_asks_the_worker_to_stream_and_reports_every_delta_verbatim() -> None:
    """The opt-in is the call's, and what reaches the caller is what the worker sent, in order."""
    with FakeStreamingWorker() as worker:
        deltas: list[str] = []

        answer = _transport(worker).stream_with_page_image(OCR_PROMPT, _page_png(), deltas.append)

    assert worker.bodies[0]["stream"] is True
    assert deltas == RECORDED_DELTAS
    assert answer.text == RECORDED_ANSWER


def test_the_finish_reason_the_worker_sent_is_surfaced_rather_than_dropped() -> None:
    """A stream cut short by the model's own token limit says so instead of looking like a whole page."""
    truncated = [frame for frame in recorded_frames() if '"content"' in frame] + [
        'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}',
        "data: [DONE]",
    ]
    with FakeStreamingWorker(truncated) as worker:
        answer = _transport(worker).stream_with_page_image(OCR_PROMPT, _page_png(), lambda _delta: None)

    assert answer.finish_reason == "length"


def test_a_blocking_call_is_unchanged_and_never_asks_for_a_stream() -> None:
    """A caller that does not stream sends today's body and reads today's single answer."""
    with FakeStreamingWorker() as worker:
        answer = _transport(worker).complete_with_page_image(OCR_PROMPT, _page_png())

    assert "stream" not in worker.bodies[0]
    assert answer == RECORDED_ANSWER


def test_a_worker_that_fails_mid_stream_is_named_in_the_worker_s_own_words() -> None:
    """The failure is the read's own, and it says what the endpoint said, not which branch noticed."""
    with FakeStreamingWorker(fail_after=5, error="the model ran out of memory") as worker, pytest.raises(
        VlmTransportError
    ) as raised:
        _transport(worker).stream_with_page_image(OCR_PROMPT, _page_png(), lambda _delta: None)

    assert "the model ran out of memory" in str(raised.value)
    assert worker.base_url + "/chat/completions" in str(raised.value)


def test_a_stream_that_ends_before_done_is_a_named_failure() -> None:
    """A connection that just stops is not a short page: nobody said this read was finished."""
    with FakeStreamingWorker(end_after=6) as worker, pytest.raises(VlmTransportError) as raised:
        _transport(worker).stream_with_page_image(OCR_PROMPT, _page_png(), lambda _delta: None)

    assert "[DONE]" in str(raised.value)


class RecordingSink:
    """A caller watching the read: every delta it was handed, and every page it was handed."""

    def __init__(self) -> None:
        self.deltas: list[tuple[int, str]] = []
        self.pages: list = []

    def on_delta(self, page_number: int, text: str) -> None:
        self.deltas.append((page_number, text))

    def on_page(self, page) -> None:
        self.pages.append(page)

    @property
    def raw(self) -> str:
        return "".join(text for _, text in self.deltas)


def _scanned_pdf(pages: int = 1) -> bytes:
    """A PDF the engine will render and send: a real page image, so the read is the real one."""
    document = pymupdf.open()
    for number in range(1, pages + 1):
        page = document.new_page(width=595, height=842)
        page.insert_text((72, 120), f"a synthetic scan, page {number}")
    content = document.tobytes()
    document.close()
    return content


def test_the_recorded_page_streams_to_exactly_the_page_text_the_adapter_exports() -> None:
    """ADR-0014's first proof: the deltas, normalized once, are the Page Text the pipeline exports."""
    sink = RecordingSink()
    with FakeStreamingWorker() as worker:
        engine = PaddleOcrVlEngine(transport=_transport(worker), dpi=72)

        extracted = engine.extract(Document(name="https://a.example/scan.pdf", content=_scanned_pdf()), sink)

    assert sink.raw == RECORDED_ANSWER, "a delta was lost or reordered on the way to the caller"
    assert [page.number for page in sink.pages] == [1]
    assert extracted.pages[0].text == PAGE_TEXT, "the page the pipeline exports is not the recorded page"
    assert strip_model_preamble(sink.raw, OCR_PROMPT) == extracted.pages[0].text
    assert sink.pages[0] == extracted.pages[0]
    assert "```" not in extracted.pages[0].text, "the fence survived into the page"
    assert not extracted.pages[0].text.startswith("OCR:"), "the echoed prompt survived into the page"


def test_a_read_with_no_sink_asks_for_no_stream_and_exports_the_same_page() -> None:
    """The batch path's read: no sink, no stream flag, and the page it always got."""
    with FakeStreamingWorker() as worker:
        engine = PaddleOcrVlEngine(transport=_transport(worker), dpi=72)

        extracted = engine.extract(Document(name="https://a.example/scan.pdf", content=_scanned_pdf()))

    assert "stream" not in worker.bodies[0]
    assert extracted.pages[0].text == PAGE_TEXT


def test_each_page_is_reported_as_it_completes_in_page_number_order() -> None:
    """The page events reach a caller in the order the Export keeps, never in arrival order."""
    sink = RecordingSink()
    with FakeStreamingWorker() as worker:
        engine = PaddleOcrVlEngine(transport=_transport(worker), dpi=72)

        extracted = engine.extract(Document(name="https://a.example/two.pdf", content=_scanned_pdf(2)), sink)

    assert [page.number for page in sink.pages] == [1, 2]
    assert tuple(sink.pages) == extracted.pages
    assert {number for number, _ in sink.deltas} == {1, 2}


def _cut_at_the_token_limit() -> list[str]:
    """The recorded page, ended the way a worker cut off by its own token limit ends a stream.

    Every frame of the page is sent, the model's own fence is never closed, and the server says why it
    stopped: ``finish_reason: "length"``, then ``[DONE]``. It is a complete stream of an incomplete page.
    """
    return [frame for frame in recorded_frames() if delta_text(frame)] + [
        'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}',
        "data: [DONE]",
    ]


def test_a_page_cut_off_at_the_token_limit_fails_the_read_in_the_worker_s_own_words() -> None:
    """ADR-0014: a page the model was cut off from is never exported as a short whole page."""
    sink = RecordingSink()
    with FakeStreamingWorker(_cut_at_the_token_limit()) as worker:
        engine = PaddleOcrVlEngine(transport=_transport(worker), dpi=72)

        with pytest.raises(PaddleOcrError) as raised:
            engine.extract(Document(name="https://a.example/scan.pdf", content=_scanned_pdf()), sink)

    words = str(raised.value)
    assert "page 1" in words, "the failure names the page that was cut off"
    assert "token limit" in words and "length" in words, "the failure names the truncation and its reason"
    assert sink.pages == [], "a page cut short is never reported to a watcher as a completed page"


def test_a_page_whose_stream_ended_with_stop_is_the_whole_page_it_always_was() -> None:
    """The reason a whole page ends with is not a failure: the exported page is the recorded one."""
    sink = RecordingSink()
    with FakeStreamingWorker() as worker:
        engine = PaddleOcrVlEngine(transport=_transport(worker), dpi=72)

        extracted = engine.extract(Document(name="https://a.example/scan.pdf", content=_scanned_pdf()), sink)

    assert extracted.pages[0].text == PAGE_TEXT, "a stop is a whole page, and normalization is unchanged"
    assert sink.pages == list(extracted.pages)


def test_a_worker_that_fails_mid_stream_fails_the_read_and_reports_no_page() -> None:
    """A page that never finished is not a page: the failure travels, and the sink is told nothing."""
    sink = RecordingSink()
    with FakeStreamingWorker(fail_after=5, error="the model ran out of memory") as worker:
        engine = PaddleOcrVlEngine(transport=_transport(worker), dpi=72)

        with pytest.raises(VlmTransportError, match="the model ran out of memory"):
            engine.extract(Document(name="https://a.example/scan.pdf", content=_scanned_pdf()), sink)

    assert sink.pages == []


def _text_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 120), "text the PDF already carries")
    content = document.tobytes()
    document.close()
    return content


def test_the_text_layer_has_nothing_to_stream_and_reports_nothing() -> None:
    """ADR-0014: a lane with no tokens shows whole. It says so by reporting nothing, not by faking it."""
    sink = RecordingSink()

    extracted = PymupdfTextEngine().extract(Document(name="https://a.example/text.pdf", content=_text_pdf()), sink)

    assert [page.number for page in extracted.pages] == [1], "the text layer still read the document"
    assert sink.deltas == []
    assert sink.pages == []


def test_a_local_command_engine_has_nothing_to_stream_and_reports_nothing(tmp_path) -> None:
    """The command writes its results when it finishes: there is no page-by-page output to report."""

    def write_result(argv: list[str]) -> None:
        output_dir = Path(argv[argv.index("--save_path") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "page-0001_res.json").write_text(
            json.dumps({"res": {"page_index": 0, "parsing_res_list": [{"block_content": "page 1 text"}]}})
        )

    sink = RecordingSink()
    engine = PaddleOcrVlCliEngine(work_dir=tmp_path, runner=RecordingRunner(on_run=write_result))

    extracted = engine.extract(Document(name="https://a.example/scan.pdf", content=_text_pdf()), sink)

    assert [page.text for page in extracted.pages] == ["page 1 text"]
    assert sink.deltas == []
    assert sink.pages == []
