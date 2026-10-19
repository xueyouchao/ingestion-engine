"""The demo's three Spend Gates: their order, their boundaries, and the words a visitor reads.

The order is ADR-0012's -- page cap, then per-client rate limit, then the daily OCR page budget -- and
the expected sentences are the spec's own, so a refusal that quotes the numbers cannot drift from them.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

import finepdfs.domain.demo as demo
from finepdfs.domain.demo import (
    DEFAULT_DAILY_OCR_PAGE_BUDGET,
    DEFAULT_PAGE_CAP,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_QUEUE_DEPTH,
    DEFAULT_RATE_LIMIT_READS,
    DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    DEFAULT_UPLOAD_BYTES,
    DEFAULT_WARM_TIMEOUT_SECONDS,
    DemoLimits,
    ReadingStage,
    RefusalKind,
    SpendGate,
    SpendGatePolicy,
    UsageSnapshot,
)


def test_the_demo_defaults_are_the_spec_s_limits() -> None:
    limits = DemoLimits()

    assert limits.page_cap == 25
    assert limits.rate_limit_reads == 5
    assert limits.rate_limit_window_seconds == 600
    assert limits.daily_ocr_page_budget == 300
    assert limits.upload_bytes == 20 * 1024 * 1024
    assert limits.queue_depth == 3
    assert limits.warm_timeout_seconds == 900
    assert limits.poll_interval_seconds == 2
    assert (DEFAULT_PAGE_CAP, DEFAULT_RATE_LIMIT_READS, DEFAULT_RATE_LIMIT_WINDOW_SECONDS) == (25, 5, 600)
    assert (DEFAULT_DAILY_OCR_PAGE_BUDGET, DEFAULT_UPLOAD_BYTES) == (300, 20 * 1024 * 1024)
    assert (DEFAULT_QUEUE_DEPTH, DEFAULT_WARM_TIMEOUT_SECONDS, DEFAULT_POLL_INTERVAL_SECONDS) == (3, 900, 2)


@pytest.mark.parametrize("page_count", [1, 24, 25])
def test_a_document_within_the_page_cap_is_admitted(page_count: int) -> None:
    decision = SpendGatePolicy().decide(page_count, UsageSnapshot())

    assert decision.open is True
    assert decision.refused is False
    assert decision.gate is None
    assert decision.reason == ""


def test_a_document_one_page_over_the_cap_is_refused_in_the_visitor_s_terms() -> None:
    decision = SpendGatePolicy().decide(26, UsageSnapshot())

    assert decision.refused is True
    assert decision.gate == "page-cap"
    assert decision.reason == "this PDF has 26 pages; the demo reads up to 25"


def test_the_page_cap_quotes_the_document_and_the_limit_that_closed() -> None:
    decision = SpendGatePolicy(DemoLimits(page_cap=25)).decide(84, UsageSnapshot())

    assert decision.reason == "this PDF has 84 pages; the demo reads up to 25"


@pytest.mark.parametrize("reads_in_window", [0, 4])
def test_a_client_under_the_rate_limit_is_admitted(reads_in_window: int) -> None:
    decision = SpendGatePolicy().decide(10, UsageSnapshot(reads_in_window=reads_in_window))

    assert decision.open is True


@pytest.mark.parametrize("reads_in_window", [5, 6])
def test_a_client_who_has_spent_the_rate_limit_is_refused_in_the_visitor_s_terms(reads_in_window: int) -> None:
    decision = SpendGatePolicy().decide(10, UsageSnapshot(reads_in_window=reads_in_window))

    assert decision.refused is True
    assert decision.gate == "rate-limit"
    assert decision.reason == f"you have read {reads_in_window} documents in the last 10 minutes; the demo allows 5"


def test_a_window_that_is_not_whole_minutes_is_stated_in_seconds() -> None:
    policy = SpendGatePolicy(DemoLimits(rate_limit_reads=1, rate_limit_window_seconds=90))

    decision = policy.decide(10, UsageSnapshot(reads_in_window=1))

    assert decision.reason == "you have read 1 documents in the last 90 seconds; the demo allows 1"


def test_the_page_cap_closes_before_the_rate_limit() -> None:
    decision = SpendGatePolicy().decide(84, UsageSnapshot(reads_in_window=5, ocr_pages_today=300))

    assert decision.gate == "page-cap"


def test_the_rate_limit_closes_before_the_daily_budget() -> None:
    policy = SpendGatePolicy()

    decision = policy.decide(20, UsageSnapshot(reads_in_window=5, ocr_pages_today=300), ocr_pages=20)

    assert decision.gate == "rate-limit"


@pytest.mark.parametrize("ocr_pages_today", [0, 275])
def test_an_ocr_read_that_fits_the_budget_is_admitted(ocr_pages_today: int) -> None:
    decision = SpendGatePolicy().decide(25, UsageSnapshot(ocr_pages_today=ocr_pages_today), ocr_pages=25)

    assert decision.open is True


@pytest.mark.parametrize("ocr_pages_today", [276, 300])
def test_an_ocr_read_that_would_exceed_the_budget_is_refused_in_the_visitor_s_terms(ocr_pages_today: int) -> None:
    decision = SpendGatePolicy().decide(25, UsageSnapshot(ocr_pages_today=ocr_pages_today), ocr_pages=25)

    assert decision.refused is True
    assert decision.gate == "daily-budget"
    assert decision.reason == (
        f"the OCR budget for today is spent: {ocr_pages_today} of 300 pages used and this PDF needs 25 more"
    )


def test_a_spent_budget_does_not_close_the_gate_for_a_native_read() -> None:
    decision = SpendGatePolicy().decide(25, UsageSnapshot(ocr_pages_today=300))

    assert decision.open is True


def test_the_refusal_kinds_and_the_reading_stages_are_the_page_s_vocabulary() -> None:
    assert get_args(SpendGate) == ("page-cap", "rate-limit", "daily-budget")
    assert get_args(RefusalKind) == (
        "page-cap",
        "rate-limit",
        "daily-budget",
        "unreadable",
        "busy",
        "unavailable",
        "failed",
    )
    assert get_args(ReadingStage) == ("queued", "routing", "warming", "reading", "done")


def test_the_policy_module_keeps_no_clock_a_file_or_a_socket() -> None:
    tree = ast.parse(Path(demo.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not [name for name in imported if name.split(".")[0] in {"time", "datetime", "socket", "pathlib", "os"}]
