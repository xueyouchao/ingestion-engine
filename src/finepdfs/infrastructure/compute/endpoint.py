"""Where a rented GPU worker is, and how a run leases one.

A worker is rented for the length of a run: start it, learn where it is listening, use it, give it
back. The endpoint is *not* the engine's identity. A provider that hands out a new host every run
(Modal, RunPod) must report a stable identity, because the memo key is the engine identity (ADR-0005):
an ephemeral URL in that key would re-read the whole corpus on every run, which is the exact cost the
memo exists to avoid.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

ENGINE_BASE_URL = "FINEPDFS_ENGINE_BASE_URL"
ENGINE_MODEL = "FINEPDFS_ENGINE_MODEL"
ENGINE_API_KEY = "FINEPDFS_ENGINE_API_KEY"
ENGINE_IDENTITY = "FINEPDFS_ENGINE_IDENTITY"


@dataclass(frozen=True, slots=True)
class GpuEndpoint:
    """A GPU worker a run may use: where to reach it, what it runs, and its stable name.

    ``identity`` is what enters the memo key. ``base_url`` is where the bytes go right now; for a
    rented worker that address is new every run and must never be mistaken for identity.
    """

    base_url: str = ""
    model: str = ""
    api_key: str = ""
    identity: str = ""

    def __bool__(self) -> bool:
        return bool(self.base_url or self.identity)


class GpuProvider(Protocol):
    """Rents the GPU a remote engine runs on. The lease lasts exactly as long as the run does."""

    def lease(self) -> AbstractContextManager[GpuEndpoint]: ...


def export_endpoint(endpoint: GpuEndpoint, environ: MutableMapping[str, str]) -> None:
    """Publish a leased endpoint as the FINEPDFS_* variables the flow already reads.

    The flow is configured entirely by the environment, because `cocoindex update main.py` takes no
    arguments of its own. A lease reaches it the same way, so every entry point is wired by one rule:
    export the endpoint, then let the existing environment path build the engine.

    Precedence is deliberate. A leased address overwrites whatever the environment held, because the
    user asked for a rented worker and that worker is where the bytes have to go. The identity is
    different: a value already set is kept, so a user who knows which worker version is running can pin
    the memo name and have it outlive the provider's derived default.
    """
    for variable, value in (
        (ENGINE_BASE_URL, endpoint.base_url),
        (ENGINE_MODEL, endpoint.model),
        (ENGINE_API_KEY, endpoint.api_key),
    ):
        if value:
            environ[variable] = value
    if endpoint.identity:
        environ.setdefault(ENGINE_IDENTITY, endpoint.identity)
