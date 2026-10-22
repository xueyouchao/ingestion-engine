"""Composing the demo's read watchers: one read, two panes.

ADR-0014's raw transcript and rendered document are separate stores with separate rules -- the transcript
keeps raw deltas and is freed when the Reading ends, the document keeps completed Page Texts and outlives
the delta stream. Neither knows about the other. This module is the seam where the composition root joins
them: a small object that forwards every report of a read to each pane, so an engine is handed one sink and
the panes stay independent.
"""

from __future__ import annotations

from finepdfs.domain.document import PageText
from finepdfs.domain.ports import PageCountSink, ReadSink


class FanOutReadSink:
    """One read's reports forwarded to every pane that asked to watch it.

    A pane may be absent -- the transcript is switched off by a zero ceiling -- and is then skipped rather
    than requiring a null object at each call site. The page count is offered only to panes that take it
    (``PageCountSink``), so a delta-only sink is still a valid watcher.
    """

    def __init__(self, *panes: ReadSink | None) -> None:
        self._panes = tuple(pane for pane in panes if pane is not None)

    def on_delta(self, page_number: int, text: str) -> None:
        for pane in self._panes:
            pane.on_delta(page_number, text)

    def on_page(self, page: PageText) -> None:
        for pane in self._panes:
            pane.on_page(page)

    def on_page_count(self, page_count: int) -> None:
        for pane in self._panes:
            if isinstance(pane, PageCountSink):
                pane.on_page_count(page_count)
