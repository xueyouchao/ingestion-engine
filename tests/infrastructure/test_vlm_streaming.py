"""The streaming page read: the opt-in wire, the verbatim report, and the page it adds up to.

ADR-0014 makes the stream opt-in and never required. A caller that does not ask keeps the blocking
answer it has today; a caller that asks gets the worker's own ``data:`` frames as they arrive. The
worker here is the shared localhost fake in ``tests/fake_streaming_worker.py``, replaying a recorded
PaddleOCR-VL page, so no network and no model are needed and the wire is the real one.
"""

from __future__ import annotations

import pytest

from finepdfs.infrastructure.engines.paddleocr_vl import OCR_PROMPT
from finepdfs.infrastructure.engines.vlm_chat import ChatEndpoint, VlmChatTransport, VlmTransportError
from tests.fake_streaming_worker import FakeStreamingWorker, recorded_frames

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


def _transport(worker: FakeStreamingWorker) -> VlmChatTransport:
    return VlmChatTransport(ChatEndpoint(base_url=worker.base_url, model="PaddleOCR-VL-1.6"))


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
