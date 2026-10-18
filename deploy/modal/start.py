"""Rent one Modal sandbox for a run, wait for its ``/v1`` surface, and print the endpoint.

This is the start command behind ``FINEPDFS_GPU_START_COMMAND`` (ADR-0009). It rents *one detached
Sandbox*, not a deployed app: a Sandbox is the per-run unit, is created from the pinned worker image,
survives this process exiting, and is terminated by id from the stop command. Nothing here calls
``modal deploy`` or leaves a function serving between runs.

Ordering is the contract:

1. take this lease's reservation -- an OS ``flock`` on the sibling lock file -- and keep it until the
   worker id is recorded: a hand-run start has no provider identity, so two of them share
   ``manual.json`` and without the reservation both pass the gate, both rent a worker, and the later
   write drops the earlier id -- a GPU billing with no handle stop can find;
2. create the Sandbox;
3. write its id to the lease state *immediately*, before any readiness wait, so a start that times out
   or fails readiness still leaves the stop command something to terminate. The file is the one named
   by this lease's identity (``FINEPDFS_GPU_LEASE_ID``), which is what keeps a concurrent run's worker
   out of reach;
4. wait for the readiness probe and verify ``/v1/models`` names the pinned model;
5. print exactly one JSON object on stdout and exit.

The stop command is not run from here on a readiness failure: ``CommandGpuProvider`` runs it after the
start command exits nonzero, and running it twice would hide which side failed. Diagnostics go to
stderr, never to stdout, and error text is redacted before it is printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from deploy.gpu.contract import (
    PROVIDER_MODAL,
    WORKER_MODEL,
    WORKER_PORT,
    LeaseState,
    WorkerContractError,
    base_url_with_v1,
    emit_endpoint,
    endpoint_payload,
    lease_file,
    lease_reservation,
    positive_seconds,
    read_lease,
    redact,
    write_lease,
)
from deploy.modal.image import HF_CACHE_PATH, HF_CACHE_VOLUME, worker_image

APP_NAME = "finepdfs-gpu-worker"
GPU_VARIABLE = "FINEPDFS_GPU_TYPE"
GPU_DEFAULT = "L4"
READY_TIMEOUT_VARIABLE = "FINEPDFS_GPU_READY_TIMEOUT"
READY_TIMEOUT_DEFAULT = 900
LIFETIME_VARIABLE = "FINEPDFS_GPU_WORKER_LIFETIME"
LIFETIME_DEFAULT = 3600
TUNNEL_TIMEOUT = 60
MODELS_TIMEOUT = 10.0

Opener = Callable[..., Any]


def main(
    environ: Mapping[str, str] | None = None,
    sdk: Any = None,
    opener: Opener | None = None,
) -> int:
    """Start one worker and print its endpoint, or report the failure on stderr and exit nonzero."""
    values = os.environ if environ is None else environ
    try:
        modal = sdk if sdk is not None else _import_sdk()
        sandbox = _create_worker(modal, values)
        base_url = _await_serving(sandbox, values, opener or _open)
        emit_endpoint(endpoint_payload(base_url))
    except Exception as error:
        print(redact(f"error: {error}", values), file=sys.stderr)
        return 1
    return 0


def _import_sdk() -> Any:
    """Import Modal at call time, so the contract and its tests never need the package installed."""
    import modal

    return modal


def _create_worker(modal: Any, environ: Mapping[str, str]) -> Any:
    """Rent one worker while holding this lease's reservation, from the gate until its id is recorded.

    The reservation is the OS lock (``lease_reservation``): it covers the read/check below and the
    ``Sandbox.create`` that follows, so two hand-run starts cannot both pass the gate. It is released
    when the lease file has the id, or when either step fails, and released by the kernel if this process
    dies first -- which is why a crashed start cannot block the lease.
    """
    path = lease_file(environ)
    with lease_reservation(path):
        return _rent_worker(modal, environ, path)


def _rent_worker(modal: Any, environ: Mapping[str, str], path: Path) -> Any:
    existing = read_lease(path)
    if existing is not None:
        raise WorkerContractError(
            f"a {existing.provider} worker ({existing.worker_id}) is already recorded at {path}; "
            "run the stop command before starting another"
        )
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    weights = modal.Volume.from_name(HF_CACHE_VOLUME, create_if_missing=True)
    sandbox = modal.Sandbox.create(
        app=app,
        image=worker_image(modal),
        gpu=environ.get(GPU_VARIABLE, GPU_DEFAULT).strip() or GPU_DEFAULT,
        encrypted_ports=[WORKER_PORT],
        readiness_probe=modal.Probe.with_tcp(WORKER_PORT),
        timeout=positive_seconds(environ, LIFETIME_VARIABLE, LIFETIME_DEFAULT),
        volumes={HF_CACHE_PATH: weights},
    )
    try:
        write_lease(
            path,
            LeaseState(provider=PROVIDER_MODAL, worker_id=sandbox.object_id),
        )
    except Exception as error:
        # The worker exists but cannot be recorded, so stop could never find it. Give it back here
        # rather than leave a GPU billing with no handle anywhere.
        _terminate_quietly(sandbox, environ)
        raise WorkerContractError(
            f"the Modal worker {sandbox.object_id} was created but its lease could not be written "
            f"to {path}: {error}"
        ) from error
    return sandbox


def _await_serving(sandbox: Any, environ: Mapping[str, str], opener: Opener) -> str:
    sandbox.wait_until_ready(
        timeout=positive_seconds(environ, READY_TIMEOUT_VARIABLE, READY_TIMEOUT_DEFAULT)
    )
    tunnels = sandbox.tunnels(timeout=TUNNEL_TIMEOUT)
    tunnel = tunnels.get(WORKER_PORT)
    if tunnel is None:
        raise WorkerContractError(f"the Modal worker exposes no tunnel on port {WORKER_PORT}")
    base_url = base_url_with_v1(tunnel.url)
    _verify_models(base_url, opener, MODELS_TIMEOUT)
    return base_url


def _verify_models(base_url: str, opener: Opener, timeout: float) -> None:
    """Check the surface the Engine will call, not just a listening port."""
    url = base_url + "/models"
    try:
        with opener(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as error:
        raise WorkerContractError(f"the worker did not answer {url}: {error}") from error
    entries = payload.get("data") if isinstance(payload, dict) else None
    served = [entry.get("id") for entry in entries or [] if isinstance(entry, dict)]
    if WORKER_MODEL not in served:
        raise WorkerContractError(f"the worker serves {served!r} at /v1/models, not {WORKER_MODEL!r}")


def _open(url: str, timeout: float) -> Any:
    return urllib.request.urlopen(url, timeout=timeout)


def _terminate_quietly(sandbox: Any, environ: Mapping[str, str]) -> None:
    try:
        sandbox.terminate()
    except Exception as error:
        warning = f"warning: could not terminate Modal worker {sandbox.object_id}: {error}"
        print(redact(warning, environ), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
