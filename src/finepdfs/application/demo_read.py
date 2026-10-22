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

import logging
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
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
    OcrWorker,
    PageCountSink,
    ReadSink,
    TextLayerProbe,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Reading:
    """What a visitor is shown after a lane read the upload.

    ``plan`` is the Routing Decision with its five features, and ``thresholds`` what they were measured
    against, so the page can show both. ``elapsed_ms`` spans the whole read, warming included; ``warm_ms``
    is the part of it spent waiting for the OCR worker to answer, measured from the first readiness
    question -- ``None`` on the native lane, which never asks one, and zero when the worker answered at
    once. It is never a guess.
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
        worker: OcrWorker,
        warm_timeout_seconds: float = DEFAULT_WARM_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        redact: Callable[[str], str] = str,
    ) -> None:
        self._extraction = ExtractionStep(probe, engines, policy)
        self._policy = policy
        self._gates = gates
        self._ledger = ledger
        # The worker is a context manager rather than an engine, so entering it is what a read decides:
        # only an admitted OCR read rents one, and a native read or a refusal rents nothing (ADR-0012).
        self._worker = worker
        self._warm_timeout_seconds = warm_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        self._sleep = sleep
        # What a failure's words are shown through: a configured address or token in an engine's error
        # is a leak, not a diagnosis, and the composition root is the one place that knows both.
        self._redact = redact

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
    def thresholds(self) -> OcrRoutingPolicy:
        """The routing thresholds every outcome carries, for callers that ask before an upload."""
        return self._policy.routing

    def read(
        self,
        content: bytes,
        client: str,
        on_stage: Callable[[ReadingStage], None],
        name: str = "upload.pdf",
        sink: ReadSink | None = None,
    ) -> ReadingOutcome:
        """Route the upload, pass the gates, read it with the lane the decision chose.

        ``sink`` watches the chosen lane's read (ADR-0014); every outcome is the same with or without one,
        and a caller that leaves it out -- the batch flow, every existing test -- is served as before.
        """
        started = self._clock()
        on_stage("routing")
        try:
            plan = self._extraction.route(content)
        except Exception as error:  # bytes the probe cannot measure are a refusal, not a crash
            return Refusal(kind="unreadable", reason=_words(error), plan=None, thresholds=self._policy.routing)

        page_count = plan.quality.page_count
        # The page cap and the rate limit are decided on the snapshot taken before this read counts
        # itself; the budget is asked only for the OCR lane, because only OCR pages cost money. One
        # decision, and the policy applies the gates in ADR-0012's order inside it. A refusal spends
        # nothing -- no read against the visitor's window, no page against the day -- so the two charges
        # below happen only after every gate has let this upload through.
        usage = self._ledger.usage(client)
        ocr_pages = page_count if plan.decision.needs_ocr else None
        closed = _closed(self._gates.decide(page_count, usage, ocr_pages=ocr_pages), plan, self._policy.routing)
        if closed is not None:
            return closed

        # The page count is routing's answer, so a pane that draws one placeholder per page can start
        # before the first delta (ADR-0014). A sink that only watches deltas is not asked for it.
        if isinstance(sink, PageCountSink):
            sink.on_page_count(page_count)

        if ocr_pages is not None:
            return self._read_ocr(plan, content, name, on_stage, started, client, ocr_pages, sink)

        self._ledger.record_read(client)
        on_stage("reading")
        try:
            extracted = self._extraction.extract_with(
                Document(name=name, content=content), plan.engine_name, sink
            )
        except Exception as error:  # a failed lane is reported, not raised: the demo answers either way
            return self._failed(plan, error)
        on_stage("done")
        return Reading(
            plan=plan,
            thresholds=self._policy.routing,
            engine=extracted.engine,
            pages=extracted.pages,
            elapsed_ms=_milliseconds(started, self._clock()),
            warm_ms=None,
        )

    def _read_ocr(
        self,
        plan: RoutingPlan,
        content: bytes,
        name: str,
        on_stage: Callable[[ReadingStage], None],
        started: float,
        client: str,
        pages: int,
        sink: ReadSink | None = None,
    ) -> ReadingOutcome:
        """The OCR lane, and the only place a worker is entered: admitted, then warmed, then read.

        Entering is separated from the body on purpose. A worker that cannot be had at all is a refusal of
        kind ``unavailable`` -- no gate closed and no lane ran -- while a worker that came up and then
        failed to read is a Failed Reading. A release that fails after the answer is the operator's log
        line, not the visitor's: the text is real either way (ADR-0007).

        The two charges are committed only once the worker exists. Charging on admission and not on
        success is deliberate -- an admitted read spends the GPU whether or not it finishes -- but a read
        whose worker never started read nothing and spent nothing, and a refusal charges nothing: a start
        command that keeps failing must not eat a visitor's window or the day's budget. Between the
        decision and the charge nothing else can interleave, because the queue runs one Reading at a time.
        """
        try:
            lease = self._worker()  # a fresh context manager: a worker belongs to one Reading
            engine, endpoint = lease.__enter__()
        except Exception as error:  # a provider that cannot start one: nothing was read, nothing was spent
            return Refusal(
                kind="unavailable",
                reason=self._redact(f"the demo could not reach its OCR worker: {_words(error)}"),
                plan=plan,
                thresholds=self._policy.routing,
            )
        outcome: ReadingOutcome | None = None
        try:
            self._ledger.record_read(client)
            self._ledger.charge_ocr_pages(pages)
            try:
                warm_ms = self._warm(on_stage, endpoint)
            except (OcrEndpointUnavailable, _WarmTimeout) as fault:
                outcome = self._failed(plan, fault)
            else:
                on_stage("reading")
                try:
                    extracted = engine.extract(Document(name=name, content=content), sink)
                except Exception as error:  # a failed lane is reported, not raised
                    outcome = self._failed(plan, error)
                else:
                    on_stage("done")
                    outcome = Reading(
                        plan=plan,
                        thresholds=self._policy.routing,
                        engine=extracted.engine,
                        pages=extracted.pages,
                        elapsed_ms=_milliseconds(started, self._clock()),
                        warm_ms=warm_ms,
                    )
        finally:
            self._release(lease)
        return outcome

    def _release(self, lease: AbstractContextManager[tuple[OcrEngine, OcrEndpointProbe]]) -> None:
        """Give the worker back, and never replace the answer with a failure to do so."""
        try:
            lease.__exit__(None, None, None)
        except Exception as error:  # the operator's report, not the visitor's
            logger.warning("the OCR worker could not be released after a Reading: %s", self._redact(_words(error)))

    def _warm(self, on_stage: Callable[[ReadingStage], None], endpoint: OcrEndpointProbe) -> int:
        """Wait for a cold worker; the measured wait in milliseconds, zero when it was already answering.

        The clock starts before the first question, because asking can start a container and block for as
        long as the probe waits -- a first check that returned after a wait is a wait, and reporting it as
        "no wait" would understate the one number this demo exists to measure. The ``warming`` stage is
        announced only when the first answer was "not yet", so a warm worker never shows a stage it did
        not have.

        Only a timeout is this method's own fault: the probe's ``OcrEndpointUnavailable`` travels up
        untouched, because waiting cannot fix a bearer it refuses or a route it does not serve.
        """
        started = self._clock()
        if endpoint.ready():
            return _milliseconds(started, self._clock())
        on_stage("warming")
        while True:
            if self._clock() - started >= self._warm_timeout_seconds:
                raise _WarmTimeout(f"the OCR worker did not answer within {self._warm_timeout_seconds:g} s")
            self._sleep(self._poll_interval_seconds)
            if endpoint.ready():
                return _milliseconds(started, self._clock())

    def _failed(self, plan: RoutingPlan, error: BaseException) -> FailedReading:
        """A lane's failure, in the error's own words and under the exclusion kind that already exists."""
        return FailedReading(
            kind="failed",
            lane=plan.lane,
            reason=self._redact(_words(error)),
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
