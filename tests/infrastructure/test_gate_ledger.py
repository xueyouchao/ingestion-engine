"""The Gate Ledger in memory: its sliding window, its UTC day, and that a charge is never given back.

The ledger is the only place time enters the demo's spend accounting. It is driven here by a hand-written
clock, so a window that prunes and a day that rolls over are observations and not sleeps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from finepdfs.domain.demo import DemoLimits, UsageSnapshot
from finepdfs.domain.ports import GateLedger
from finepdfs.infrastructure.demo.ledger import InMemoryGateLedger


class FakeClock:
    """A clock a test moves by hand; it is the only time any ledger in this file sees."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _ledger(now: datetime | None = None, **limits: object) -> tuple[InMemoryGateLedger, FakeClock]:
    clock = FakeClock(now or datetime(2026, 10, 19, 12, 0, tzinfo=UTC))
    return InMemoryGateLedger(clock, DemoLimits(**limits)), clock


def test_a_client_with_no_history_has_spent_nothing() -> None:
    ledger, _ = _ledger()

    assert ledger.usage("10.0.0.1") == UsageSnapshot(reads_in_window=0, ocr_pages_today=0)


def test_a_recorded_read_is_in_the_client_s_window() -> None:
    ledger, _ = _ledger()

    ledger.record_read("10.0.0.1")
    ledger.record_read("10.0.0.1")

    assert ledger.usage("10.0.0.1").reads_in_window == 2


def test_each_client_has_its_own_window() -> None:
    ledger, _ = _ledger()

    ledger.record_read("10.0.0.1")

    assert ledger.usage("10.0.0.2").reads_in_window == 0


def test_a_read_inside_the_window_is_still_counted() -> None:
    ledger, clock = _ledger()

    ledger.record_read("10.0.0.1")
    clock.advance(599)

    assert ledger.usage("10.0.0.1").reads_in_window == 1


def test_a_read_that_reaches_the_window_edge_has_been_pruned() -> None:
    ledger, clock = _ledger()

    ledger.record_read("10.0.0.1")
    clock.advance(600)

    assert ledger.usage("10.0.0.1").reads_in_window == 0


def test_the_window_keeps_the_reads_that_are_still_inside_it() -> None:
    ledger, clock = _ledger()

    ledger.record_read("10.0.0.1")
    clock.advance(300)
    ledger.record_read("10.0.0.1")
    clock.advance(400)

    assert ledger.usage("10.0.0.1").reads_in_window == 1


def test_ocr_pages_are_charged_to_today() -> None:
    ledger, _ = _ledger()

    ledger.charge_ocr_pages(25)
    ledger.charge_ocr_pages(13)

    assert ledger.usage("10.0.0.1").ocr_pages_today == 38


def test_a_new_utc_day_starts_the_budget_again_without_a_background_task() -> None:
    ledger, clock = _ledger(datetime(2026, 10, 19, 23, 59, tzinfo=UTC))
    ledger.charge_ocr_pages(300)

    clock.advance(120)

    assert ledger.usage("10.0.0.1").ocr_pages_today == 0
    ledger.charge_ocr_pages(4)
    assert ledger.usage("10.0.0.1").ocr_pages_today == 4


def test_a_page_charged_across_midnight_lands_on_the_day_it_was_charged() -> None:
    ledger, clock = _ledger(datetime(2026, 10, 19, 23, 59, 30, tzinfo=UTC))
    ledger.charge_ocr_pages(10)

    clock.advance(24 * 3600)

    assert ledger.usage("10.0.0.1").ocr_pages_today == 0


def test_the_daily_budget_is_the_demo_s_and_not_one_client_s() -> None:
    ledger, _ = _ledger()

    ledger.charge_ocr_pages(25)

    assert ledger.usage("10.0.0.2").ocr_pages_today == 25


def test_the_ledger_offers_no_way_to_give_a_charge_back() -> None:
    # Charging happens on admission, because an admitted read spent the GPU whether or not it finished.
    assert not hasattr(GateLedger, "refund")
    assert not hasattr(InMemoryGateLedger, "refund")
