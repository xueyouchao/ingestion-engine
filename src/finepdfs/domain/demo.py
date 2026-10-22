"""What the live upload demo allows a visitor to spend, and what it says when a Spend Gate closes.

ADR-0012 bounds the demo with three Spend Gates in one order: a page cap on the upload, then a rate
limit per client, then a daily budget of OCR pages. The order lives here, in one pure policy, so two
callers cannot disagree about which gate closed. The counters the policy reads are a snapshot handed in
by the caller; time belongs to the Gate Ledger behind the port, and nothing here imports a clock, a file
or a socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# The demo's own limits, each a default the composition root may override from the environment. The last
# four are not spend at all -- they shape the upload, the Reading Queue and a warm read -- but they are the
# demo's settings and they live beside the rest so one module answers "what does the demo allow?".
DEFAULT_PAGE_CAP = 25
DEFAULT_RATE_LIMIT_READS = 5
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 600
DEFAULT_DAILY_OCR_PAGE_BUDGET = 300
DEFAULT_UPLOAD_BYTES = 20 * 1024 * 1024
DEFAULT_QUEUE_DEPTH = 3
DEFAULT_WARM_TIMEOUT_SECONDS = 900
DEFAULT_POLL_INTERVAL_SECONDS = 2
# ADR-0014's raw transcript: how many raw bytes one page may keep in memory, and how long a quiet
# event stream waits before a heartbeat. Both are demo settings, so they live beside the rest.
DEFAULT_TRANSCRIPT_BYTES = 256 * 1024
DEFAULT_HEARTBEAT_SECONDS = 15

# The gate that closed, what the page refuses with, and where a Reading stands. These are the page's
# vocabulary: the kinds and stage names are the exact strings the demo server and the page exchange.
SpendGate = Literal["page-cap", "rate-limit", "daily-budget"]
RefusalKind = Literal[
    "page-cap", "rate-limit", "daily-budget", "upload", "unreadable", "busy", "unavailable", "failed"
]
ReadingStage = Literal["queued", "routing", "warming", "reading", "done"]


@dataclass(frozen=True, slots=True)
class DemoLimits:
    """What the demo allows. One setting per limit, so the spend posture is configuration, not code."""

    page_cap: int = DEFAULT_PAGE_CAP
    rate_limit_reads: int = DEFAULT_RATE_LIMIT_READS
    rate_limit_window_seconds: float = DEFAULT_RATE_LIMIT_WINDOW_SECONDS
    daily_ocr_page_budget: int = DEFAULT_DAILY_OCR_PAGE_BUDGET
    upload_bytes: int = DEFAULT_UPLOAD_BYTES
    queue_depth: int = DEFAULT_QUEUE_DEPTH
    warm_timeout_seconds: float = DEFAULT_WARM_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    transcript_bytes: int = DEFAULT_TRANSCRIPT_BYTES
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """What one visitor and the demo as a whole have spent so far, as the ledger reported it.

    ``reads_in_window`` counts the client's reads inside the rate-limit window; ``ocr_pages_today``
    counts every OCR page charged since the current UTC day began, across all clients, because the
    budget is the demo's and not a visitor's.
    """

    reads_in_window: int = 0
    ocr_pages_today: int = 0


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether the gates let this upload through, and if not, which gate closed and the visitor's words."""

    open: bool
    gate: SpendGate | None = None
    reason: str = ""

    @property
    def refused(self) -> bool:
        return not self.open


@dataclass(frozen=True, slots=True)
class SpendGatePolicy:
    """The three Spend Gates, applied in ADR-0012's order, stopping at the first that closes."""

    limits: DemoLimits = DemoLimits()

    def decide(self, page_count: int, usage: UsageSnapshot, ocr_pages: int | None = None) -> GateDecision:
        """Judge one upload against ``usage``.

        ``ocr_pages`` is how many pages the chosen lane would send to OCR, or ``None`` when the lane is
        the text layer. Only OCR pages cost money, so the daily budget is consulted only when
        ``ocr_pages`` is given; a native read is admitted even after the budget is spent.
        """
        if page_count > self.limits.page_cap:
            return GateDecision(
                open=False,
                gate="page-cap",
                reason=(
                    f"this document has {_count(page_count, 'page')}; "
                    f"the demo reads up to {self.limits.page_cap}"
                ),
            )
        if usage.reads_in_window >= self.limits.rate_limit_reads:
            return GateDecision(
                open=False,
                gate="rate-limit",
                reason=(
                    f"you have read {_count(usage.reads_in_window, 'document')} in the last "
                    f"{_window_phrase(self.limits.rate_limit_window_seconds)}; "
                    f"the demo allows {self.limits.rate_limit_reads}"
                ),
            )
        if ocr_pages is not None and usage.ocr_pages_today + ocr_pages > self.limits.daily_ocr_page_budget:
            return GateDecision(
                open=False,
                gate="daily-budget",
                reason=(
                    f"the OCR budget for today is spent: {usage.ocr_pages_today} of "
                    f"{_count(self.limits.daily_ocr_page_budget, 'page')} used and this document "
                    f"needs {ocr_pages} more"
                ),
            )
        return GateDecision(open=True)


def _window_phrase(seconds: float) -> str:
    """The rate-limit window as a visitor reads it: whole minutes when it is one, seconds otherwise."""
    if seconds >= 60 and seconds % 60 == 0:
        return _count(seconds / 60, "minute")
    return _count(seconds, "second")


def _count(value: float, noun: str) -> str:
    """A number and its noun, singular when the number is one: "1 document", "10 minutes"."""
    return f"{value:g} {noun}" if value == 1 else f"{value:g} {noun}s"

