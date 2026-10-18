"""What one run actually did, counted where it happened.

The extract body is memoised on (content digest, engine name, engine identity), so it runs only on a
miss (ADR-0005). That makes the counters a property of the run, not of CocoIndex's component graph:

* a *call* is counted inside the memoised body, so it is one actual Extraction and not a memo hit;
* a *reuse* is a memo key the reading pass asked for that the body never ran for;
* the counters start fresh at the beginning of every update, so a runtime reused across updates
  reports the run that just finished, not the sum of the runs before it.

CocoIndex's own component and memo statistics are deliberately not folded in here; they count work
units, not engine calls. The summary is written by the flow as declared target state, so no second
writer exists beside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = 1

# The exclusion kinds the flow can declare. The summary keeps a zero for each even when a run has
# none, so a reader can depend on the shape instead of on which failures happened this time.
EXCLUSION_KINDS = ("dropped", "failed", "exact-duplicate", "near-duplicate")

_ExtractionKey = tuple[str, str, str]


@dataclass
class RunMetrics:
    """One run's counters, carried by the Runtime that the flow reads.

    Mutable on purpose: the flow's concurrent reading pass and its deciding pass both report into
    the run that owns it. Each update is a single assignment on the event loop, so no lock is needed.
    """

    routed_native: int = 0
    routed_ocr: int = 0
    published_documents: int = 0
    page_records: int = 0
    exclusions: dict[str, int] = field(default_factory=lambda: dict.fromkeys(EXCLUSION_KINDS, 0))
    _requested: dict[_ExtractionKey, None] = field(default_factory=dict)
    _executed: dict[_ExtractionKey, None] = field(default_factory=dict)

    def reset(self) -> None:
        """Start a new update's counters.

        One Runtime can serve several updates: the demo keeps a warm database and
        ``configure_runtime`` holds the runtime between commands. Each update is a run of its own, so
        its counters start from zero instead of carrying the previous run's work into its summary.
        """
        self.routed_native = 0
        self.routed_ocr = 0
        self.published_documents = 0
        self.page_records = 0
        self.exclusions = dict.fromkeys(EXCLUSION_KINDS, 0)
        self._requested.clear()
        self._executed.clear()

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
        """The declared counters, and nothing that makes two identical runs differ.

        No timestamp, duration, host path or lease id lands here: a published record must be
        reproducible, so an unchanged rerun rewrites these exact bytes and target state does not
        change. Wall-clock observation belongs to the command wrapper that measures the run, and an
        engine whose identity probe cannot answer is reported as ``unavailable`` by the caller.
        """
        return {
            "schema_version": SCHEMA_VERSION,
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
