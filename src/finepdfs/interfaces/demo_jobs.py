"""The Reading Queue: one Reading at a time, a bounded wait behind it, and the job a page polls.

ADR-0012 makes an upload a job rather than a long request. The upload answers with an id, the page asks
that id for its stage, and the stages are the ``ReadingStage`` names the domain already carries. A
single slot is what makes the per-Reading environment lease safe -- a lease mutates ``os.environ``, so
two Readings at once would build one engine's endpoint inside another's -- and it is what ADR-0012
means when it says the demo server's queue, not Modal, serialises uploads.

A bounded number wait behind the one running, and a submission that finds every place taken is refused
with its own kind rather than queued indefinitely. The most recent finished jobs are kept so a page that
polls a moment late still gets its answer; nothing survives the process, and the uploaded bytes are
dropped as soon as the Reading that owned them finished (only page images ever leave the host, ADR-0012).

This module is the queue alone: no HTTP, no clock, no lease. ``demo_server`` composes it.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, Thread
from typing import Any
from uuid import uuid4

from finepdfs.application.demo_read import FailedReading, Reading, ReadingOutcome, Refusal
from finepdfs.domain.demo import DemoLimits, ReadingStage
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.domain.ports import ReadSink
from finepdfs.interfaces.demo_transcript import RawTranscript

# What the page receives when an upload finds the single slot and its waiting list taken. ``busy`` is the
# RefusalKind the domain already names for "the demo is reading someone else's document".
QUEUE_FULL_KIND = "busy"
QUEUE_FULL_REASON = "the demo is reading someone else's document right now; try again in a moment"

# How many finished jobs stay answerable. A page polls within seconds of finishing, so this only has to
# outlive a reload; it is not job state the demo promises to keep across a restart (ADR-0012).
DEFAULT_RETAINED_JOBS = 32

# A reader is what ``DemoRead.read`` is: bytes, a client identity, a stage reporter, a document name and
# the sink the job's raw transcript is written to (``None`` when the transcript is switched off).
Reader = Callable[[bytes, str, Callable[[ReadingStage], None], str, ReadSink | None], ReadingOutcome]


class QueueFull(Exception):
    """Every place in the Reading Queue is taken, so the upload was refused before it cost anything.

    The kind and the sentence are the page's own words, carried here so the server answers with this
    refusal without restating the reason in a second place.
    """

    kind = QUEUE_FULL_KIND

    def __init__(self, reason: str = QUEUE_FULL_REASON) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ReadingJob:
    """One upload's standing as the page sees it: the stage it is at, and at the end its outcome.

    A snapshot taken under the queue's lock, never the live entry: a handler thread and the reader
    thread must not read and write the same fields at once. ``failure`` is the queue's own record for a
    reader that raised before the use case could turn it into one of its three outcomes.
    """

    id: str
    name: str
    stage: ReadingStage
    outcome: ReadingOutcome | None
    failure: dict[str, str] | None = None

    def as_json(self) -> dict[str, Any]:
        """The job exactly as ``GET /api/read/<id>`` answers it: one shape, with nulls where it is absent."""
        reading = self.outcome if isinstance(self.outcome, Reading) else None
        return {
            "job": self.id,
            "stage": self.stage,
            "outcome": _outcome(self.outcome),
            "elapsed_ms": reading.elapsed_ms if reading is not None else None,
            "warmed_ms": reading.warm_ms if reading is not None else None,
            "refusal": _refusal(self.outcome),
            "failure": _failure(self.outcome) or self.failure,
            "reading": _reading(reading),
        }


@dataclass(slots=True)
class _Entry:
    """The live entry the worker thread mutates; a ``ReadingJob`` is a snapshot of it."""

    id: str
    name: str
    content: bytes
    client: str
    stage: ReadingStage = "queued"
    outcome: ReadingOutcome | None = None
    failure: dict[str, str] | None = None
    # The job's raw transcript (ADR-0014): created with the job and released the moment it finishes, so
    # the store never outlives the bytes it describes.
    transcript: RawTranscript | None = None
    # The sink the engine is actually handed: one object forwarding to every pane watching this job
    # (the composition root builds it), or ``None`` when no pane watches this job at all.
    sink: ReadSink | None = None


class ReadingQueue:
    """One Reading at a time, ``queue_depth`` waiting, and the most recent finished jobs answerable."""

    def __init__(
        self,
        read: Reader,
        limits: DemoLimits | None = None,
        retained_jobs: int = DEFAULT_RETAINED_JOBS,
        *,
        redact: Callable[[str], str] = str,
        sinks: Callable[[str, RawTranscript | None], ReadSink] | None = None,
        finished: Callable[[str, ReadingOutcome | None], None] | None = None,
    ) -> None:
        self._read = read
        settings = limits if limits is not None else DemoLimits()
        self._depth = settings.queue_depth
        # Zero means the operator switched the transcript off: no store, and the events endpoint says so.
        self._transcript_bytes = settings.transcript_bytes
        self._redact = redact
        # How the composition root joins the panes: one sink per job, and what it hears when the Reading
        # ends. The queue owns the job id and the transcript; everything else is the composition's.
        self._sinks = sinks
        self._on_finished = finished
        self._retained_jobs = max(retained_jobs, 0)
        self._lock = Condition()
        self._waiting: deque[_Entry] = deque()
        self._running: _Entry | None = None
        self._finished: OrderedDict[str, ReadingJob] = OrderedDict()
        self._closed = False
        self._worker: Thread | None = None

    def submit(self, content: bytes, client: str, name: str = "upload.pdf") -> ReadingJob:
        """Queue one upload, or refuse it with ``QueueFull`` when the waiting list has no place left.

        Only a submission that would be the fourth, fifth ... behind a running Reading is refused: with
        no Reading in flight a new upload always starts, because there is nothing to wait behind. The
        decision and the insert happen under one lock, so two simultaneous uploads cannot both count a
        free place.
        """
        with self._lock:
            waiting = len(self._waiting)
            if (waiting or self._running is not None) and waiting >= self._depth:
                raise QueueFull()
            job_id = uuid4().hex
            transcript = RawTranscript(self._transcript_bytes) if self._transcript_bytes > 0 else None
            entry = _Entry(
                id=job_id,
                name=name,
                content=content,
                client=client,
                transcript=transcript,
                sink=self._sinks(job_id, transcript) if self._sinks else transcript,
            )
            self._waiting.append(entry)
            self._start_worker()
            self._lock.notify()
            return _snapshot(entry)

    def get(self, job_id: str) -> ReadingJob | None:
        """The job's standing, or ``None`` for an id this queue never issued or no longer keeps."""
        with self._lock:
            for entry in self._waiting:
                if entry.id == job_id:
                    return _snapshot(entry)
            if self._running is not None and self._running.id == job_id:
                return _snapshot(self._running)
            return self._finished.get(job_id)

    def transcript(self, job_id: str) -> RawTranscript | None:
        """The running or waiting job's raw transcript, or ``None`` once it finished or was forgotten."""
        with self._lock:
            for entry in self._waiting:
                if entry.id == job_id:
                    return entry.transcript
            if self._running is not None and self._running.id == job_id:
                return self._running.transcript
            return None

    def depth(self) -> dict[str, int]:
        """How many Readings are running and how many wait behind them, for the status answer."""
        with self._lock:
            return {"active": 1 if self._running is not None else 0, "waiting": len(self._waiting)}

    def close(self) -> None:
        """Stop taking work and let the worker thread go; the process is ending or the test is done."""
        with self._lock:
            self._closed = True
            for entry in self._waiting:
                if entry.transcript is not None:
                    entry.transcript.finish(None)
                entry.transcript = None
                entry.sink = None
            self._waiting.clear()
            self._lock.notify_all()
            worker = self._worker
        if worker is not None:
            worker.join(timeout=5)

    def _start_worker(self) -> None:
        """Start the one worker thread on first use, so an unused queue costs no thread."""
        if self._worker is None:
            self._worker = Thread(target=self._work, name="finepdfs-demo-read", daemon=True)
            self._worker.start()

    def _work(self) -> None:
        """Take one job at a time, run its Reading, and keep its outcome answerable."""
        while True:
            with self._lock:
                while not self._waiting and not self._closed:
                    self._lock.wait()
                if self._closed:
                    return
                entry = self._waiting.popleft()
                self._running = entry

            outcome, failure = self._read_one(entry)
            if self._on_finished is not None:
                self._on_finished(entry.id, outcome)
            self._finish_transcript(entry, outcome, failure)

            with self._lock:
                entry.outcome = outcome
                entry.failure = failure
                entry.stage = "done"
                # The bytes and the transcript were this Reading's, not the queue's: nothing about the
                # document outlives it (ADR-0014 keeps the transcript ephemeral).
                entry.content = b""
                entry.transcript = None
                entry.sink = None
                self._running = None
                self._remember(_snapshot(entry))
                self._lock.notify_all()

    def _read_one(self, entry: _Entry) -> tuple[ReadingOutcome | None, dict[str, str] | None]:
        """The use case's outcome, or the queue's own failure record when the reader itself raised.

        ``DemoRead.read`` answers every outcome the demo knows, so reaching this catch means the
        composition around it broke. That is still a finished job and not a wedged queue: the page is
        told, in the error's own words, and the next upload is admitted.
        """

        def on_stage(stage: ReadingStage) -> None:
            # ``done`` is the queue's to publish, and it publishes it under the lock together with the
            # outcome: a poll in between must never see a job that says it finished and has no answer,
            # which is why the use case's own ``done`` is not taken here.
            if stage == "done":
                return
            with self._lock:
                entry.stage = stage

        try:
            return self._read(entry.content, entry.client, on_stage, entry.name, entry.sink), None
        except Exception as error:  # the queue must answer, whatever the composition did
            return None, {"kind": "failed", "lane": "", "reason": _words(error)}

    def _finish_transcript(
        self,
        entry: _Entry,
        outcome: ReadingOutcome | None,
        failure: dict[str, str] | None,
    ) -> None:
        """Tell the transcript how the read ended, in the same words the job answer will carry."""
        transcript = entry.transcript
        if transcript is None:
            return
        if failure is not None:
            transcript.fail(failure["kind"], failure.get("lane", ""), self._redact(failure["reason"]))
        elif isinstance(outcome, FailedReading):
            transcript.fail(outcome.kind, outcome.lane, self._redact(outcome.reason))
        transcript.finish("failed" if failure is not None else _outcome(outcome))

    def _remember(self, job: ReadingJob) -> None:
        """Keep the most recent finished jobs, forgetting the oldest beyond that bound."""
        if self._retained_jobs == 0:
            return
        self._finished[job.id] = job
        while len(self._finished) > self._retained_jobs:
            self._finished.popitem(last=False)


def _snapshot(entry: _Entry) -> ReadingJob:
    """The live entry as the page's frozen view of it."""
    return ReadingJob(
        id=entry.id,
        name=entry.name,
        stage="done" if entry.outcome is not None or entry.failure is not None else entry.stage,
        outcome=entry.outcome,
        failure=entry.failure,
    )


def _outcome(outcome: ReadingOutcome | None) -> str | None:
    if isinstance(outcome, Reading):
        return "read"
    if isinstance(outcome, Refusal):
        return "refused"
    if isinstance(outcome, FailedReading):
        return "failed"
    return None


def _refusal(outcome: ReadingOutcome | None) -> dict[str, str] | None:
    if isinstance(outcome, Refusal):
        return {"kind": outcome.kind, "reason": outcome.reason}
    return None


def _failure(outcome: ReadingOutcome | None) -> dict[str, str] | None:
    if isinstance(outcome, FailedReading):
        return {"kind": outcome.kind, "lane": outcome.lane, "reason": outcome.reason}
    return None


def _reading(reading: Reading | None) -> dict[str, Any] | None:
    """The Reading as the page shows it: the decision, its evidence, the engine and the pages.

    The pages keep the numbers the lane read them at -- original, 1-based -- so a citation still means
    the page the visitor is looking at, whatever the engine returned in.
    """
    if reading is None:
        return None
    pages = sorted(reading.pages, key=lambda page: page.number)
    return {
        "lane": reading.plan.lane,
        "reason": reading.plan.decision.reason,
        "features": dict(reading.plan.decision.features),
        "thresholds": _thresholds(reading.thresholds),
        "engine": str(reading.engine),
        "page_count": len(pages),
        "character_count": sum(len(page.text) for page in pages),
        "pages": [{"number": page.number, "text": page.text} for page in pages],
    }


def _thresholds(policy: OcrRoutingPolicy) -> dict[str, float]:
    """The thresholds the features were measured against, exactly as the policy holds them."""
    return {
        "min_characters_per_page": policy.min_characters_per_page,
        "min_text_coverage": policy.min_text_coverage,
        "max_garbled_ratio": policy.max_garbled_ratio,
        "max_image_dominated_share": policy.max_image_dominated_share,
    }


def _words(error: BaseException) -> str:
    """An error in its own words: never a summary, and a class name when it said nothing."""
    return str(error) or type(error).__name__
