"""How many engine calls one run may have in flight at once (ADR-0010 step 3).

An **engine call** is the leaf: one remote page request, or one local command, for every engine that
makes one. The bound belongs to the worker, not to this machine's render capacity, and it is one bound
for the whole run -- the Engine instance is shared across Documents, so a pool or a permit created per
Document would let a run reach Documents x limit. Rendering is not throttled by it.

It is deliberately not a permit a Document body takes before waiting for its own pages: with N Documents
holding all N permits and each waiting on work that needs one, the run deadlocks. The bound sits where
the call happens, and the thread that renders is never one of the workers, so no waiter can occupy a
slot its own work needs.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

ENGINE_CONCURRENCY = "FINEPDFS_ENGINE_CONCURRENCY"
DEFAULT_ENGINE_CONCURRENCY = 6


class MisconfiguredEngineConcurrency(ValueError):
    """Raised when the configured number of engine calls in flight is not a positive whole number."""


def engine_concurrency(raw: str) -> int:
    """The number of engine calls the run may keep in flight, from the configured value.

    An unset or blank value means the default, the way every other optional FINEPDFS_* variable reads
    empty as absent. Anything else has to be a positive whole number: a typo must not quietly pick a
    number, because the wrong number either serialises a GPU job or floods the worker's CPU, and the
    message names both the variable and the value it rejected so the typo is found at once.
    """
    text = raw.strip()
    if not text:
        return DEFAULT_ENGINE_CONCURRENCY
    try:
        limit = int(text)
    except ValueError:
        limit = 0
    return _at_least_one_engine_call(limit, repr(text))


def _at_least_one_engine_call(limit: int, rejected: str) -> int:
    """One message for the one rule, whichever side of the wiring asked: the parser or the bound itself."""
    if limit < 1:
        raise MisconfiguredEngineConcurrency(
            f"{ENGINE_CONCURRENCY} must be a positive whole number of engine calls in flight: {rejected}"
        )
    return limit


class EngineCalls:
    """The run's one bound on engine calls in flight, shared by every Document one Engine reads.

    Every shape of an engine call is bounded here, so "engine calls in flight" means the same thing for
    every adapter: ``submit`` queues a blocking request on a pool sized to the bound (the remote
    engine's page requests), and ``permit`` is the same bound as a plain slot (the one local command an
    adapter runs, PaddleOCR-VL's, Docling's or MinerU's, and MinerU's one HTTP request). Neither is taken
    by a Document body: the bound is held only for the duration of the call itself, so a Document
    waiting for its own pages never occupies a slot its pages need. ``limit`` is the number's one home:
    an adapter that sizes a window or a pool reads it here instead of keeping a copy.
    """

    def __init__(self, limit: int = DEFAULT_ENGINE_CONCURRENCY) -> None:
        self.limit = _at_least_one_engine_call(limit, repr(limit))
        self._slots = threading.BoundedSemaphore(self.limit)
        self._pool: ThreadPoolExecutor | None = None
        self._pool_lock = threading.Lock()

    def submit(self, call: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        """Take the call to a pool that exists for the run and is exactly this many calls wide."""
        return self._executor().submit(call, *args, **kwargs)

    def permit(self) -> threading.BoundedSemaphore:
        """One call in flight: used as a context manager around the call, and only around it."""
        return self._slots

    def _executor(self) -> ThreadPoolExecutor:
        # One pool for the Engine's lifetime, created on first use: created inside a Document's read it
        # would be one pool per Document, which is the Documents x limit ADR-0010 rejects.
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix="engine-call")
            return self._pool
