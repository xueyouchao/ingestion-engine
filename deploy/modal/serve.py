"""The deployed OCR worker app: one container, scale to zero, one bearer (ADR-0012).

This is the other GPU posture, beside ADR-0009's per-run Sandbox. ``modal deploy`` turns this module into
one Modal Server that serves the pinned worker image as an OpenAI-compatible endpoint: one container as
the ceiling, zero as the resting floor, a low-minute scaledown window, and a bearer from a Modal Secret
that vLLM checks on every ``/v1`` route. Modal's own proxy auth is off because the pipeline's adapter
sends ``Authorization: Bearer`` and cannot send ``Modal-Key``/``Modal-Secret`` (ADR-0012).

Two structural rules:

* the app is built at module scope, because ``modal deploy`` needs to find ``app`` here;
* the SDK is imported inside ``_import_sdk`` and called once at module scope, so the offline suite can
  build the whole app against a fake installed as ``sys.modules["modal"]`` and needs no vendor package.

The hooks are the cold-start contract. ``@modal.enter`` spawns the shared launcher, waits for the worker's
own ``/v1/models`` with the bearer, and only then reports the container ready, so no request lands on a
container whose vLLM is still loading weights and the startup timeout is the honest bound on a cold start.
``@modal.exit`` stops the process. Nothing here is authorised by ``src/``: the demo reaches it through the
same ``ChatEndpoint`` a leased worker uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from deploy.gpu.contract import (
    DEPLOYED_APP_NAME,
    DEPLOYED_EXIT_GRACE_PERIOD,
    DEPLOYED_GPU,
    DEPLOYED_MAX_CONTAINERS,
    DEPLOYED_MIN_CONTAINERS,
    DEPLOYED_SCALEDOWN_WINDOW,
    DEPLOYED_SECRET_NAME,
    DEPLOYED_SECRET_VARIABLE,
    DEPLOYED_SERVER_NAME,
    DEPLOYED_STARTUP_TIMEOUT,
    WORKER_MODEL,
    WORKER_PORT,
    base_url_with_v1,
)
from deploy.gpu.server import API_KEY_VARIABLE, DEFAULT_PORT, PORT_VARIABLE
from deploy.modal.image import HF_CACHE_PATH, HF_CACHE_VOLUME, WORKER_ENTRYPOINT, worker_image

# The worker listens on the container's loopback; Modal's proxy terminates TLS in front of it.
WORKER_HOST = "127.0.0.1"
READY_POLL_SECONDS = 5.0
MODELS_TIMEOUT = 10.0
STOP_TIMEOUT_SECONDS = 30


class ServeConfigurationError(RuntimeError):
    """Raised when the deployed app cannot start a worker it would be safe to serve."""


def _import_sdk() -> Any:
    """Import Modal at call time, so importing this module needs no vendor package."""
    import modal

    return modal


# Module scope on purpose: ``modal deploy`` looks for ``app`` in this module.
modal = _import_sdk()
app = modal.App(DEPLOYED_APP_NAME)
_worker_volume = modal.Volume.from_name(HF_CACHE_VOLUME, create_if_missing=True)
_worker_secret = modal.Secret.from_name(
    DEPLOYED_SECRET_NAME,
    required_keys=[DEPLOYED_SECRET_VARIABLE],
)


@app.server(
    name=DEPLOYED_SERVER_NAME,
    port=WORKER_PORT,
    image=worker_image(modal),
    gpu=DEPLOYED_GPU,
    volumes={HF_CACHE_PATH: _worker_volume},
    secrets=[_worker_secret],
    max_containers=DEPLOYED_MAX_CONTAINERS,
    min_containers=DEPLOYED_MIN_CONTAINERS,
    scaledown_window=DEPLOYED_SCALEDOWN_WINDOW,
    startup_timeout=DEPLOYED_STARTUP_TIMEOUT,
    exit_grace_period=DEPLOYED_EXIT_GRACE_PERIOD,
    unauthenticated=True,
)
class OcrWorkerServer:
    """One container running the pinned worker, started in ``@modal.enter`` and stopped in ``@modal.exit``."""

    process: Any = None

    @modal.enter()
    def start(self) -> None:
        self.process = start_worker(os.environ)

    @modal.exit()
    def stop(self) -> None:
        if self.process is not None:
            stop_worker(self.process)


def bearer(environ: Mapping[str, str]) -> str:
    """The bearer the Secret injected. Absent means refuse to serve: an open GPU endpoint is a bill."""
    value = environ.get(API_KEY_VARIABLE, "").strip()
    if not value:
        raise ServeConfigurationError(
            f"the deployed app must inject {API_KEY_VARIABLE} from the Secret {DEPLOYED_SECRET_NAME!r}"
        )
    return value


def worker_base_url(environ: Mapping[str, str]) -> str:
    """The worker's OpenAI base URL inside the container, always ending at ``/v1``."""
    port = environ.get(PORT_VARIABLE, DEFAULT_PORT).strip() or DEFAULT_PORT
    return base_url_with_v1(f"http://{WORKER_HOST}:{port}")


def start_worker(
    environ: Mapping[str, str],
    *,
    popen: Callable[..., Any] = subprocess.Popen,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """Spawn the shared launcher and wait until its ``/v1/models`` answers with the bearer.

    The launcher is the batch worker's own: it builds the pinned vLLM command and passes the bearer
    through when the Secret set one, so this posture and ADR-0009's cannot drift. A worker that never
    becomes ready is stopped here rather than left running, and the container then fails its start.
    """
    api_key = bearer(environ)
    process = popen(list(WORKER_ENTRYPOINT), env=dict(environ))
    try:
        wait_for_models(worker_base_url(environ), api_key, opener=opener or _open, sleep=sleep, clock=clock)
    except Exception:
        stop_worker(process)
        raise
    return process


def wait_for_models(
    base_url: str,
    api_key: str,
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: int = DEPLOYED_STARTUP_TIMEOUT,
    poll: float = READY_POLL_SECONDS,
) -> None:
    """Poll ``/v1/models`` with the bearer until the pinned model is served, or fail on the timeout.

    Readiness is the surface the pipeline calls, not a listening port: a container whose vLLM opened its
    socket but cannot answer would otherwise accept the first upload and fail it.
    """
    url = base_url.rstrip("/") + "/models"
    deadline = clock() + timeout
    failure: Exception | None = None
    probe = opener or _open
    while True:
        try:
            with probe(url, timeout=MODELS_TIMEOUT, api_key=api_key) as response:
                payload = json.loads(response.read().decode("utf-8"))
            served = _served_models(payload)
            if WORKER_MODEL not in served:
                raise ServeConfigurationError(
                    f"the worker serves {served!r} at /v1/models, not {WORKER_MODEL!r}"
                )
            return
        except ServeConfigurationError:
            raise
        except (OSError, ValueError) as error:
            failure = error
        if clock() >= deadline:
            raise ServeConfigurationError(
                f"the worker did not answer {url} within {timeout} s: {failure}"
            ) from failure
        sleep(poll)


def stop_worker(process: Any) -> None:
    """Stop the spawned launcher, and its vLLM child, without waiting for ever."""
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _served_models(payload: object) -> list[str]:
    entries = payload.get("data") if isinstance(payload, dict) else None
    return [entry.get("id") for entry in entries or [] if isinstance(entry, dict)]


def _open(url: str, timeout: float, api_key: str) -> Any:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    return urllib.request.urlopen(request, timeout=timeout)
