"""The demo's read fan-out: one watched read, two panes.

The transcript and the document pane are separate stores with separate rules (ADR-0014): the transcript
keeps raw deltas verbatim and is ephemeral; the document keeps completed Page Texts and outlives the delta
stream. Composing them is the composition root's job, so this object is deliberately empty of policy: it
forwards every report and knows neither store.
"""

from __future__ import annotations

from finepdfs.domain.document import PageText
from finepdfs.interfaces.demo_sinks import FanOutReadSink


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def on_delta(self, page_number: int, text: str) -> None:
        self.calls.append(("delta", (page_number, text)))

    def on_page(self, page: PageText) -> None:
        self.calls.append(("page", page))

    def on_page_count(self, page_count: int) -> None:
        self.calls.append(("page_count", page_count))


def test_a_fan_out_sink_reports_every_delta_page_and_page_count_to_both_panes() -> None:
    transcript, document = _Recorder(), _Recorder()
    sink = FanOutReadSink(transcript, document)

    sink.on_page_count(2)
    sink.on_delta(1, "OCR")
    sink.on_page(PageText(number=1, text="one"))

    assert transcript.calls == document.calls
    assert transcript.calls == [
        ("page_count", 2),
        ("delta", (1, "OCR")),
        ("page", PageText(number=1, text="one")),
    ]


def test_a_pane_that_is_switched_off_is_skipped_rather_than_forgotten() -> None:
    document = _Recorder()
    sink = FanOutReadSink(None, document)

    sink.on_page_count(1)
    sink.on_delta(1, "text")
    sink.on_page(PageText(number=1, text="one"))

    assert document.calls == [
        ("page_count", 1),
        ("delta", (1, "text")),
        ("page", PageText(number=1, text="one")),
    ]
