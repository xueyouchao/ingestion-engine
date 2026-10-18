"""Terminate the Modal worker the start command recorded, and forget it only once it is gone.

This is the stop command behind ``FINEPDFS_GPU_STOP_COMMAND`` (ADR-0009). It must be safe to run when
the worker never became ready (the id is written before readiness on purpose), when it is already
terminated, and twice in a row: the lease file is the single source of truth, and a missing file means
there is nothing to give back. A stop that cannot terminate the worker exits nonzero and *keeps* the
lease, so the operator can retry instead of losing the only handle on a GPU that may still be billing.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from deploy.gpu.contract import (
    PROVIDER_MODAL,
    LeaseState,
    WorkerContractError,
    clear_lease,
    read_lease,
    redact,
    state_path,
)


def main(environ: Mapping[str, str] | None = None, sdk: Any = None) -> int:
    """Terminate the recorded worker, or report why it could not be given back."""
    values = os.environ if environ is None else environ
    path = state_path(values)
    try:
        state = read_lease(path)
        if state is None:
            print(f"no Modal worker is recorded at {path}; nothing to stop", file=sys.stderr)
            return 0
        _require_our_lease(state)
        modal = sdk if sdk is not None else _import_sdk()
        terminated = _terminate(modal, state.worker_id)
        clear_lease(path)
        outcome = "terminated" if terminated else "was already gone; cleared the lease for"
        print(f"{outcome} Modal worker {state.worker_id}", file=sys.stderr)
    except Exception as error:
        print(redact(f"error: {error}", values), file=sys.stderr)
        return 1
    return 0


def _import_sdk() -> Any:
    """Import Modal at call time, so the offline contract tests need no vendor package."""
    import modal

    return modal


def _require_our_lease(state: LeaseState) -> None:
    """Refuse a lease another vendor's stop command owns, rather than guessing what its id means."""
    if state.provider != PROVIDER_MODAL:
        raise WorkerContractError(
            f"the recorded worker is a {state.provider} worker, not a Modal one; "
            "run that provider's stop command instead"
        )


def _terminate(modal: Any, worker_id: str) -> bool:
    """Terminate the sandbox, returning ``False`` when it was already gone.

    ``Sandbox.terminate`` is documented as a no-op for a finished sandbox, and ``from_id`` raises
    Modal's ``NotFoundError`` for an id the server no longer knows. Both mean "not billing", which is
    what stop promises; any other failure is a real failure and must stay visible.
    """
    try:
        sandbox = modal.Sandbox.from_id(worker_id)
    except Exception as error:
        if _is_not_found(modal, error):
            return False
        raise
    try:
        sandbox.terminate()
    except Exception as error:
        if _is_not_found(modal, error):
            return False
        raise
    return True


def _is_not_found(modal: Any, error: BaseException) -> bool:
    not_found = getattr(getattr(modal, "exception", None), "NotFoundError", None)
    return isinstance(not_found, type) and isinstance(error, not_found)


if __name__ == "__main__":
    raise SystemExit(main())
