"""Reading one uploaded document for the live demo: route it, pass the gates, wait for the worker, read.

This is the demo's only use case, and it is deliberately thin. Routing is ``ExtractionStep`` from
``application/curate.py``, not a second copy of the policy, so a threshold change moves the demo and the
batch run together. The Spend Gates are ``SpendGatePolicy``, asked in ADR-0012's order. What the demo adds
is the shape of the answer: one of three outcomes, because a Reading, a Refusal and a Failed Reading carry
different fields and a caller that switches on the type cannot read a page list that is not there.

Nothing here cleans, identifies a language, publishes, deduplicates or writes a file: the demo is a view
of Extraction and its Decision, not a curation run (ADR-0012).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from finepdfs.application.curate import CurationPolicy, ExtractionStep, RoutingPlan
from finepdfs.domain.demo import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_WARM_TIMEOUT_SECONDS,
    GateDecision,
    ReadingStage,
    RefusalKind,
    SpendGatePolicy,
)
from finepdfs.domain.document import Document, EngineIdentity, PageText
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.domain.ports import (
    GateLedger,
    OcrEndpointProbe,
    OcrEndpointUnavailable,
    OcrEngine,
    TextLayerProbe,
)


@dataclass(frozen=True, slots=True)
class Reading:
    """What a visitor is shown after a lane read the upload.

    ``plan`` is the Routing Decision with its five features, and ``thresholds`` what they were measured
    against, so the page can show both. ``elapsed_ms`` spans the whole read, warming included; ``warm_ms``
    is the part of it spent waiting for a cold OCR worker, measured, or ``None`` when nothing was waited
    for -- it is never a guess.
    """

    plan: RoutingPlan
    thresholds: OcrRoutingPolicy
    engine: EngineIdentity
    pages: tuple[PageText, ...]
    elapsed_ms: int
    warm_ms: int | None


@dataclass(frozen=True, slots=True)
class Refusal:
    """The demo declined before or instead of spending. No page text is here, because none was read.

    ``kind`` is one of the Spend Gates or, when no gate closed, the kind of upload that could not be read;
    the plan is absent only when routing never got far enough to make one.
    """

    kind: RefusalKind
    reason: str
    plan: RoutingPlan | None
    thresholds: OcrRoutingPolicy


@dataclass(frozen=True, slots=True)
class FailedReading:
    """A lane was chosen and the read failed: ``lane`` names it and ``reason`` is the error's own words.

    ``kind`` is the exclusion kind the pipeline already uses for a failure, so a failed Reading says the
    same thing about a document that a batch run does.
    """

    kind: Literal["failed"]
    lane: str
    reason: str
    plan: RoutingPlan | None
    thresholds: OcrRoutingPolicy


ReadingOutcome = Reading | Refusal | FailedReading


class _WarmTimeout(Exception):
    """The endpoint was asked for the whole warm timeout and never answered: the worker did not come up."""


class DemoRead:
    """One uploaded PDF in, one Reading (or a Refusal, or a Failed Reading) out."""

    def __init__(
        self,
        probe: TextLayerProbe,
        engines: Mapping[str, OcrEngine],
        policy: CurationPolicy,
        gates: SpendGatePolicy,
        ledger: GateLedger,
        endpoint: OcrEndpointProbe,
        warm_timeout_seconds: float = DEFAULT_WARM_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._extraction = ExtractionStep(probe, engines, policy)
        self._policy = policy
        self._gates = gates
        self._ledger = ledger
        self._endpoint = endpoint
        self._warm_timeout_seconds = warm_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        self._sleep = sleep

    @property
    def extraction(self) -> ExtractionStep:
        return self._extraction

    @property
    def gates(self) -> SpendGatePolicy:
        return self._gates

    @property
    def ledger(self) -> GateLedger:
        return self._ledger

    @property
    def endpoint(self) -> OcrEndpointProbe:
        return self._endpoint

    @property
    def thresholds(self) -> OcrRoutingPolicy:
        """The routing thresholds every outcome carries, for callers that ask before an upload."""
        return self._policy.routing

    def read(
        self,
        content: bytes,
        client: str,
        on_stage: Callable[[ReadingStage], None],
        name: str = "upload.pdf",
    ) -> ReadingOutcome:
        """Route the upload, pass the gates, read it with the lane the decision chose."""
        started = self._clock()
        warm_ms: int | None = None
        on_stage("routing")
        try:
            plan = self._extraction.route(content)
        except Exception as error:  # bytes the probe cannot measure are a refusal, not a crash
            return Refusal(kind="unreadable", reason=_words(error), plan=None, thresholds=self._policy.routing)

        # The page cap and the rate limit, on the snapshot taken before this read counts itself: asking
        # again after recording would count this read against the visitor who is making it.
        page_count = plan.quality.page_count
        usage = self._ledger.usage(client)
        closed = _closed(self._gates.decide(page_count, usage, ocr_pages=None), plan, self._policy.routing)
        if closed is not None:
            return closed
        self._ledger.record_read(client)

        if plan.decision.needs_ocr:
            # Only OCR pages are billed, and they are charged once they are admitted: an admitted read
            # spends the GPU whether or not it finishes, and nothing here can give a charge back.
            closed = _closed(
                self._gates.decide(page_count, usage, ocr_pages=page_count), plan, self._policy.routing
            )
            if closed is not None:
                return closed
            self._ledger.charge_ocr_pages(page_count)
            try:
                warm_ms = self._warm(on_stage)
            except (OcrEndpointUnavailable, _WarmTimeout) as fault:
                return self._failed(plan, fault)

        on_stage("reading")
        try:
            extracted = self._extraction.extract_with(Document(name=name, content=content), plan.engine_name)
        except Exception as error:  # a failed lane is reported, not raised: the demo answers either way
            return self._failed(plan, error)
        on_stage("done")
        return Reading(
            plan=plan,
            thresholds=self._policy.routing,
            engine=extracted.engine,
            pages=extracted.pages,
            elapsed_ms=_milliseconds(started, self._clock()),
            warm_ms=warm_ms,
        )

    def _warm(self, on_stage: Callable[[ReadingStage], None]) -> int | None:
        """Wait for a cold OCR endpoint; the measured wait in milliseconds, or None if it was answering.

        The first question is asked before the warming stage is announced, so a warm worker never shows a
        stage it did not have. Only a timeout is a fault here: the endpoint probe's own
        ``OcrEndpointUnavailable`` travels up untouched, because waiting cannot fix a bearer it refuses.
        """
        if self._endpoint.ready():
            return None
        on_stage("warming")
        started = self._clock()
        while True:
            if self._clock() - started >= self._warm_timeout_seconds:
                raise _WarmTimeout(f"the OCR worker did not answer within {self._warm_timeout_seconds:g} s")
            self._sleep(self._poll_interval_seconds)
            if self._endpoint.ready():
                return _milliseconds(started, self._clock())

    def _failed(self, plan: RoutingPlan, error: BaseException) -> FailedReading:
        """A lane's failure, in the error's own words and under the exclusion kind that already exists."""
        return FailedReading(
            kind="failed",
            lane=plan.engine_name,
            reason=_words(error),
            plan=plan,
            thresholds=self._policy.routing,
        )


def _closed(decision: GateDecision, plan: RoutingPlan, thresholds: OcrRoutingPolicy) -> Refusal | None:
    """The Refusal a closed gate earns, or ``None`` when the gate let the read through.

    A ``GateDecision`` names its gate exactly when it refuses, so a refused decision always carries the
    gate's own kind and sentence, and the page never has to guess which gate it hit.
    """
    if decision.refused and decision.gate is not None:
        return Refusal(kind=decision.gate, reason=decision.reason, plan=plan, thresholds=thresholds)
    return None


def _words(error: BaseException) -> str:
    """An error in its own words: never a summary, and a class name when it said nothing."""
    return str(error) or type(error).__name__


def _milliseconds(started: float, finished: float) -> int:
    """A measured span in whole milliseconds, never negative: ``monotonic`` can only go forward."""
    return round(max(finished - started, 0.0) * 1000)
