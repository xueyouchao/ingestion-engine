"""The demo's spend counters, kept in the process and lost with it.

The Gate Ledger is the one place in the demo where time enters spend accounting, which is why the clock
is injected rather than read: a test moves it by hand and observes a window that prunes and a day that
rolls over. Both happen lazily, on the next answer the ledger gives, so there is no scheduler and no
background task, and a restart forgets the day's spend (stated in the runbook, not fixed here).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from threading import Lock

from finepdfs.domain.demo import DemoLimits, UsageSnapshot


class InMemoryGateLedger:
    """One process's worth of Gate Ledger, for the demo server's life.

    A read enters the client's window and stays there while it is strictly younger than the window, so a
    read made exactly one window ago no longer counts. OCR pages are charged to the UTC day they were
    charged on; a charge after midnight starts the new day's spend from that day's first page. The demo
    server handles requests on threads, so every answer is taken under one lock.
    """

    def __init__(self, clock: Callable[[], datetime], limits: DemoLimits | None = None) -> None:
        self._clock = clock
        self._limits = limits if limits is not None else DemoLimits()
        self._lock = Lock()
        self._reads: dict[str, list[datetime]] = {}
        self._day: date | None = None
        self._ocr_pages_today = 0

    def usage(self, client: str) -> UsageSnapshot:
        with self._lock:
            now = self._clock()
            self._roll_over(now)
            return UsageSnapshot(reads_in_window=len(self._prune(now, client)), ocr_pages_today=self._ocr_pages_today)

    def record_read(self, client: str) -> None:
        with self._lock:
            now = self._clock()
            self._prune(now, client)
            self._reads[client].append(now)

    def charge_ocr_pages(self, pages: int) -> None:
        with self._lock:
            self._roll_over(self._clock())
            self._ocr_pages_today += pages

    def _prune(self, now: datetime, client: str) -> list[datetime]:
        """Drop the client's reads that have reached the window's edge; return the ones still inside it."""
        cutoff = now - timedelta(seconds=self._limits.rate_limit_window_seconds)
        reads = [at for at in self._reads.get(client, ()) if at > cutoff]
        self._reads[client] = reads
        return reads

    def _roll_over(self, now: datetime) -> None:
        today = now.astimezone(UTC).date()
        if self._day != today:
            self._day = today
            self._ocr_pages_today = 0
