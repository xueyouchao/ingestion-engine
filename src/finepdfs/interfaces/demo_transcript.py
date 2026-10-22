"""The live demo's raw transcript: what the worker emitted, kept bounded and served as events.

ADR-0014 makes the raw read proof of work, not evidence. A Reading hands every delta to this store from
the engine-call pool thread; the store keeps a per-page ring buffer with a byte ceiling and lets any
number of subscribers replay it from the beginning. Nothing here is written to disk, put in the job
answer or cited, and the queue releases the store the moment the Reading finishes.

The wire contract, which the page asset and the HTTP tests depend on. Every event is an SSE
``event:``/``data:`` pair whose data is one compact JSON object:

    delta      {"page": 1, "text": "OCR"}        one raw delta, verbatim, tagged with its 1-based page
    truncated  {"page": 1, "dropped_bytes": 12}  this page's buffer dropped its oldest bytes; the count
                                                 is cumulative for the page and no dropped text is kept
    page       {"number": 1, "text": "# Report"} the Page Text the read exported for that page
    error      {"kind": "failed", "lane": "ocr", "reason": "..."} the read failed, in the same redacted
                                                 words the job answer carries
    done       {"outcome": "read"}               the last event; ``outcome`` is "read", "refused",
                                                 "failed" or null

A heartbeat is the SSE comment line ``: heartbeat`` sent while no event has arrived for one heartbeat
interval. The stream ends after ``done`` (or ``error`` when a finish follows it); a browser must close
the EventSource on ``done`` rather than letting it reconnect. A late or reloaded subscriber is replayed
from the store's start, which for a truncated page is the truncated event plus the retained tail.

The store is ephemeral by construction: an absent job id is answered with an HTTP error before a stream
starts, and a finished Reading's store is detached by the queue, so there is nothing left to replay.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition
from typing import Any

from finepdfs.domain.document import PageText

# How many raw bytes one page may keep. Memory per active job is bounded by this ceiling times the page
# cap, and the whole store is freed when the job finishes. The server names the same number as a demo
# limit, so it is configuration rather than a constant a deployment cannot change.
DEFAULT_TRANSCRIPT_BYTES = 256 * 1024
# How long a quiet stream waits before writing a heartbeat comment. The server exposes it as a limit too.
DEFAULT_HEARTBEAT_SECONDS = 15.0
# How long one blocking write may take before a subscriber is treated as gone. A client that stops
# reading its socket must not pin a handler thread forever; this is the timeout that ends it.
STREAM_WRITE_TIMEOUT_SECONDS = 30.0

HEARTBEAT = b": heartbeat\n\n"


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """One retained event, with the sequence number a subscriber uses as its replay cursor."""

    sequence: int
    name: str
    data: dict[str, Any]


class RawTranscript:
    """A bounded per-page record of one Reading's raw deltas, safe for the pool and the handler threads.

    The ceiling is per page, not per job: deltas for several pages interleave (ADR-0010), so one long
    page must not evict another page's read. A page that outruns its ceiling keeps its newest bytes and
    loses the start, which is reported as a ``truncated`` event naming the byte count rather than as a
    silently shortened page. ``ceiling_bytes=0`` keeps no text at all and reports every page truncated.
    """

    def __init__(self, ceiling_bytes: int = DEFAULT_TRANSCRIPT_BYTES) -> None:
        self._ceiling = max(int(ceiling_bytes), 0)
        self._lock = Condition()
        self._events: OrderedDict[int, TranscriptEvent] = OrderedDict()
        self._page_events: dict[int, deque[int]] = defaultdict(deque)
        self._page_bytes: dict[int, int] = defaultdict(int)
        self._dropped: dict[int, int] = defaultdict(int)
        self._truncated: dict[int, int] = {}
        self._sequence = 0
        self._finished = False

    # -- ReadSink, called from the engine-call pool threads -------------------------------------------

    def on_delta(self, page_number: int, text: str) -> None:
        """Keep one raw delta under the page's ceiling; drop the oldest bytes when it does not fit."""
        if not text:
            return
        raw = text.encode("utf-8")
        with self._lock:
            if self._finished:
                return
            kept = self._tail(raw) if len(raw) > self._ceiling else text
            dropped = len(raw) - len(kept.encode("utf-8"))
            while self._page_bytes[page_number] + len(kept.encode("utf-8")) > self._ceiling:
                oldest = self._page_events[page_number].popleft()
                gone = self._events.pop(oldest)
                size = len(str(gone.data["text"]).encode("utf-8"))
                self._page_bytes[page_number] -= size
                dropped += size
            if dropped:
                self._mark_truncated(page_number, dropped)
            if kept:
                event = self._append("delta", {"page": page_number, "text": kept})
                self._page_events[page_number].append(event.sequence)
                self._page_bytes[page_number] += len(kept.encode("utf-8"))
            self._lock.notify_all()

    def on_page(self, page: PageText) -> None:
        """Keep the Page Text the read exported, exactly as the job answer will carry it."""
        with self._lock:
            if self._finished:
                return
            self._append("page", {"number": page.number, "text": page.text})
            self._lock.notify_all()

    # -- lifecycle, called by the Reading Queue ------------------------------------------------------

    def fail(self, kind: str, lane: str, reason: str) -> None:
        """Record that the read failed, in the words the job answer already shows."""
        with self._lock:
            if self._finished:
                return
            self._append("error", {"kind": kind, "lane": lane, "reason": reason})
            self._lock.notify_all()

    def finish(self, outcome: str | None) -> None:
        """Mark the read finished: append the terminal event and wake every subscriber."""
        with self._lock:
            if self._finished:
                return
            self._append("done", {"outcome": outcome})
            self._finished = True
            self._lock.notify_all()

    # -- subscribers ---------------------------------------------------------------------------------

    @property
    def finished(self) -> bool:
        with self._lock:
            return self._finished

    def since(self, cursor: int = 0) -> tuple[list[TranscriptEvent], bool]:
        """Every retained event after ``cursor``, and whether the read has finished."""
        with self._lock:
            return self._after(cursor), self._finished

    def wait(self, cursor: int, timeout: float) -> tuple[list[TranscriptEvent], bool]:
        """Wait for an event after ``cursor`` or for the read to finish, up to ``timeout`` seconds."""
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._lock:
            while not self._finished and self._sequence <= cursor:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lock.wait(remaining)
            return self._after(cursor), self._finished

    # -- internals, all under ``self._lock`` ---------------------------------------------------------

    def _tail(self, raw: bytes) -> str:
        """The newest ``ceiling`` bytes of one delta, decoded without inventing characters."""
        if self._ceiling == 0:
            return ""
        return raw[-self._ceiling :].decode("utf-8", errors="ignore")

    def _mark_truncated(self, page_number: int, dropped: int) -> None:
        """Name the drop: replace this page's marker with one carrying the cumulative byte count."""
        self._dropped[page_number] += dropped
        previous = self._truncated.get(page_number)
        if previous is not None:
            self._events.pop(previous, None)
        event = self._append("truncated", {"page": page_number, "dropped_bytes": self._dropped[page_number]})
        self._truncated[page_number] = event.sequence

    def _append(self, name: str, data: dict[str, Any]) -> TranscriptEvent:
        self._sequence += 1
        event = TranscriptEvent(sequence=self._sequence, name=name, data=data)
        self._events[event.sequence] = event
        return event

    def _after(self, cursor: int) -> list[TranscriptEvent]:
        return [event for sequence, event in self._events.items() if sequence > cursor]


def encode_event(event: TranscriptEvent) -> bytes:
    """One event as the SSE frame a browser reads: the name, the JSON data, then the blank line."""
    return f"event: {event.name}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n".encode()


def stream_events(
    transcript: RawTranscript,
    write: Callable[[bytes], object],
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
) -> None:
    """Write the transcript to ``write`` until it finishes, heartbeating while the read is quiet.

    This is the bounded loop behind the SSE endpoint: it blocks only for one heartbeat interval at a
    time, never for the whole read, so a subscriber that stops reading is noticed by the writer's own
    socket timeout and a finished read always ends the loop.
    """
    cursor = 0
    while True:
        events, finished = transcript.since(cursor)
        for event in events:
            write(encode_event(event))
            cursor = event.sequence
        if finished:
            return
        events, finished = transcript.wait(cursor, heartbeat_seconds)
        if not events and not finished:
            write(HEARTBEAT)
