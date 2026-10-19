"""How many engine calls one run may have in flight at once (ADR-0010 step 3).

An **engine call** is the leaf: one remote page request, or one local `doc_parser` invocation. The bound
belongs to the worker, not to this machine's render capacity, and it is one bound for the whole run --
the Engine instance is shared across Documents, so a pool or a permit created per Document would let a
run reach Documents x limit. Rendering is not throttled by it.

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
    if limit < 1:
        raise MisconfiguredEngineConcurrency(
            f"{ENGINE_CONCURRENCY} must be a positive whole number of engine calls in flight: {text!r}"
        )
    return limit


class EngineCalls:
    """The run's one bound on engine calls in flight, shared by every Document one Engine reads.

    Both shapes of an engine call are bounded here, so "engine calls in flight" means the same thing
    for both adapters: ``submit`` queues a blocking request on a pool sized to the bound (the remote
    engine's page requests), and ``permit`` is the same bound as a plain slot (the CLI engine's one
    command). Neither is taken by a Document body: the bound is held only for the duration of the call
    itself, so a Document waiting for its own pages never occupies a slot its pages need.
    """

    def __init__(self, limit: int = DEFAULT_ENGINE_CONCURRENCY) -> None:
        if limit < 1:
            raise MisconfiguredEngineConcurrency(f"an engine gets at least one call in flight, not {limit!r}")
        self.limit = limit
        self._slots = threading.BoundedSemaphore(limit)
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
