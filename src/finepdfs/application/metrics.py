"""What one run actually did, counted where it happened.

The extract body is memoised on (content digest, engine name, engine identity), so it runs only on a
miss (ADR-0005). That makes the counters a property of the run, not of CocoIndex's component graph:

* a *call* is counted inside the memoised body, so it is one actual Extraction and not a memo hit;
* a *reuse* is a memo key the reading pass asked for that the body never ran for.

CocoIndex's own component and memo statistics are deliberately not folded in here; they count work
units, not engine calls. The summary is written by the flow as declared target state, so no second
writer exists beside it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

SCHEMA_VERSION = 1

# The exclusion kinds the flow can declare. The summary keeps a zero for each even when a run has
# none, so a reader can depend on the shape instead of on which failures happened this time.
EXCLUSION_KINDS = ("dropped", "failed", "exact-duplicate", "near-duplicate")

_ExtractionKey = tuple[str, str, str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunMetrics:
    """One run's counters, carried by the Runtime that the flow reads.

    Mutable on purpose: the flow's concurrent reading pass and its deciding pass both report into
    the run that owns it. Each update is a single assignment on the event loop, so no lock is needed.
    """

    started_at: float = field(default_factory=time.monotonic)
    started_wall: datetime = field(default_factory=_utc_now)
    finished_at: float | None = None
    finished_wall: datetime | None = None
    routed_native: int = 0
    routed_ocr: int = 0
    published_documents: int = 0
    page_records: int = 0
    exclusions: dict[str, int] = field(default_factory=lambda: dict.fromkeys(EXCLUSION_KINDS, 0))
    _requested: dict[_ExtractionKey, None] = field(default_factory=dict)
    _executed: dict[_ExtractionKey, None] = field(default_factory=dict)

    def route(self, *, needs_ocr: bool) -> None:
        """Count one routed Document; an exact duplicate is skipped before routing and not counted."""
        if needs_ocr:
            self.routed_ocr += 1
        else:
            self.routed_native += 1

    def request_extraction(self, digest: str, engine_name: str, engine_identity: str) -> None:
        """Record the memo key this run is about to ask CocoIndex for."""
        self._requested[(digest, engine_name, engine_identity)] = None

    def count_extraction(self, digest: str, engine_name: str, engine_identity: str) -> None:
        """Count one actual engine call. Called from inside the memoised body, so only on a miss."""
        self._executed[(digest, engine_name, engine_identity)] = None

    def count_published(self, pages: int) -> None:
        self.published_documents += 1
        self.page_records += pages

    def count_exclusion(self, kind: str) -> None:
        self.exclusions[kind] = self.exclusions.get(kind, 0) + 1

    @property
    def extraction_requested(self) -> int:
        return len(self._requested)

    @property
    def extraction_calls(self) -> int:
        return len(self._executed)

    @property
    def extractions_reused(self) -> int:
        """Requested memo keys the body did not run for, i.e. observed reuse (not a CocoIndex stat)."""
        return len(self._requested.keys() - self._executed.keys())

    def summary(
        self,
        *,
        crawl_id: str,
        domain: str,
        crawl_records: int,
        documents: int,
        engine_name: str,
        engine_identity: str,
    ) -> dict:
        """The run summary, in the shape a read-only browser can consume without recomputing anything."""
        self.finished_at = time.monotonic()
        self.finished_wall = _utc_now()
        return {
            "schema_version": SCHEMA_VERSION,
            "run": {
                "started_at": _isoformat(self.started_wall),
                "finished_at": _isoformat(self.finished_wall),
                "elapsed_seconds": round(max(self.finished_at - self.started_at, 0.0), 3),
            },
            "corpus": {
                "crawl_id": crawl_id,
                "domain": domain,
                "crawl_records": crawl_records,
                "documents": documents,
            },
            "routing": {
                "documents": self.routed_native + self.routed_ocr,
                "native": self.routed_native,
                "ocr": self.routed_ocr,
            },
            "extraction": {
                "requested": self.extraction_requested,
                "calls": self.extraction_calls,
                "reused": self.extractions_reused,
            },
            "published": {"documents": self.published_documents, "page_records": self.page_records},
            "exclusions": {
                "total": sum(self.exclusions.values()),
                "by_kind": dict(sorted(self.exclusions.items())),
            },
            "engine": {"name": engine_name, "identity": engine_identity},
        }


def _isoformat(moment: datetime | None) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if moment else ""
