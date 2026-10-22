"""The Reading Queue: one Reading at a time, a bounded wait behind it, and the job a page polls.

ADR-0012 makes an upload a job rather than a long request. The upload answers with an id, the page asks
that id for its stage, and the stages are the ``ReadingStage`` names the domain already carries. A
single slot is what makes the per-Reading environment lease safe -- a lease mutates ``os.environ``, so
two Readings at once would build one engine's endpoint inside another's -- and it is what ADR-0012
means when it says the demo server's queue, not Modal, serialises uploads.

A bounded number wait behind the one running, and a submission that finds every place taken is refused
with its own kind rather than queued indefinitely. The most recent finished jobs are kept so a page that
polls a moment late still gets its answer; beyond that one bound a job and everything it held are
forgotten together, and nothing survives the process.

This module is the queue alone: the single slot, the admission decision, the waiting list and the
retention bound. What a *job* has -- its stage, its answer, its raw transcript and its document -- is the
``DemoJob`` this module hands out, so the queue names no pane and the server joins nothing to answer one;
no HTTP, no clock, no lease. ``demo_server`` composes it.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, Thread
from uuid import uuid4

from finepdfs.application.demo_read import FailedReading, ReadingOutcome
from finepdfs.domain.demo import DemoLimits, ReadingStage
from finepdfs.domain.ports import ReadSink
from finepdfs.interfaces.demo_job import DemoJob

# What the page receives when an upload finds the single slot and its waiting list taken. ``busy`` is the
# RefusalKind the domain already names for "the demo is reading someone else's document".
QUEUE_FULL_KIND = "busy"
QUEUE_FULL_REASON = "the demo is reading someone else's document right now; try again in a moment"

# How many finished jobs stay answerable -- and with them their document, since the job holds it. A page
# polls within seconds of finishing, so this only has to outlive a reload; it is not job state the demo
# promises to keep across a restart (ADR-0012).
DEFAULT_RETAINED_JOBS = 32

# A reader is what ``DemoRead.read`` is: bytes, a client identity, a stage reporter, a document name and
# the sink the job's own state is written to (``None`` when the caller watches nothing).
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


@dataclass(slots=True)
class _Waiting:
    """One admitted upload: its job, and the bytes and client identity the job does not keep.

    The bytes are dropped the moment the Reading that owned them finished (only page images ever leave
    the host, ADR-0012), so the queue holds nothing of a document but its job.
    """

    job: DemoJob
    content: bytes
    client: str


class ReadingQueue:
    """One Reading at a time, ``queue_depth`` waiting, and the most recent finished jobs answerable."""

    def __init__(
        self,
        read: Reader,
        limits: DemoLimits | None = None,
        retained_jobs: int = DEFAULT_RETAINED_JOBS,
        *,
        redact: Callable[[str], str] = str,
    ) -> None:
        self._read = read
        settings = limits if limits is not None else DemoLimits()
        self._depth = settings.queue_depth
        # Zero means the operator switched the transcript off: no store, and the events endpoint says so.
        self._transcript_bytes = settings.transcript_bytes
        self._redact = redact
        self._retained_jobs = max(retained_jobs, 0)
        self._lock = Condition()
        self._waiting: deque[_Waiting] = deque()
        self._running: _Waiting | None = None
        self._finished: OrderedDict[str, DemoJob] = OrderedDict()
        self._closed = False
        self._worker: Thread | None = None

    def submit(self, content: bytes, client: str, name: str = "upload.pdf") -> DemoJob:
        """Queue one upload and answer its job, or refuse it with ``QueueFull`` when no place is left.

        Only a submission that would be the fourth, fifth ... behind a running Reading is refused: with
        no Reading in flight a new upload always starts, because there is nothing to wait behind. The
        decision and the insert happen under one lock, so two simultaneous uploads cannot both count a
        free place.
        """
        with self._lock:
            waiting = len(self._waiting)
            if (waiting or self._running is not None) and waiting >= self._depth:
                raise QueueFull()
            job = DemoJob(
                uuid4().hex,
                name,
                transcript_bytes=self._transcript_bytes,
                redact=self._redact,
            )
            self._waiting.append(_Waiting(job=job, content=content, client=client))
            self._start_worker()
            self._lock.notify()
        return job

    def job(self, job_id: str) -> DemoJob | None:
        """The job this id names, while it waits, runs or is still remembered; ``None`` after that."""
        with self._lock:
            for waiting in self._waiting:
                if waiting.job.id == job_id:
                    return waiting.job
            if self._running is not None and self._running.job.id == job_id:
                return self._running.job
            return self._finished.get(job_id)

    def depth(self) -> dict[str, int]:
        """How many Readings are running and how many wait behind them, for the status answer."""
        with self._lock:
            return {"active": 1 if self._running is not None else 0, "waiting": len(self._waiting)}

    def close(self) -> None:
        """Stop taking work and let the worker thread go; the process is ending or the test is done."""
        with self._lock:
            self._closed = True
            for waiting in self._waiting:
                waiting.job.finish(None)
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
        """Take one job at a time, run its Reading, and keep its answer answerable."""
        while True:
            with self._lock:
                while not self._waiting and not self._closed:
                    self._lock.wait()
                if self._closed:
                    return
                waiting = self._waiting.popleft()
                self._running = waiting

            outcome, broken = self._read_one(waiting)
            # The job publishes its answer and frees what it held, under its own lock, before the queue
            # stops naming it as the one running: a poll in between finds the finished job rather than a
            # gap in which its id means nothing.
            waiting.job.finish(outcome, broken)

            with self._lock:
                # The bytes were this Reading's, not the queue's: nothing about the document outlives it.
                waiting.content = b""
                self._running = None
                self._remember(waiting.job)
                self._lock.notify_all()

    def _read_one(self, waiting: _Waiting) -> tuple[ReadingOutcome | None, FailedReading | None]:
        """The use case's outcome, or the job's own failure when the reader itself raised.

        ``DemoRead.read`` answers every outcome the demo knows, so reaching this catch means the
        composition around it broke. That is still a finished job and not a wedged queue: the page is
        told, in the error's own words, and the next upload is admitted.
        """
        job = waiting.job
        try:
            return self._read(waiting.content, waiting.client, job.advance, job.name, job), None
        except Exception as error:  # the queue must answer, whatever the composition did
            return None, FailedReading.broke(error)

    def _remember(self, job: DemoJob) -> None:
        """Keep the most recent finished jobs, forgetting the oldest beyond that one bound."""
        if self._retained_jobs == 0:
            return
        self._finished[job.id] = job
        while len(self._finished) > self._retained_jobs:
            self._finished.popitem(last=False)
