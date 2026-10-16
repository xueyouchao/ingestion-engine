"""The GPU providers a run can choose from: no rented GPU, a fixed server, or one a command rents.

The last one is the seam that lets a Modal or RunPod worker be injected without this package knowing
anything about either: the start command prints where its worker is listening, the run uses it, and the
stop command gives it back. Provider SDKs, when they are wanted, are just another command or another
adapter behind the same two methods.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from finepdfs.infrastructure.compute.endpoint import GpuEndpoint, GpuProvider
from finepdfs.infrastructure.engines.subprocess_support import (
    CommandFailed,
    CommandRunner,
    SubprocessRunner,
    require_success,
)

GPU_PROVIDER = "FINEPDFS_GPU_PROVIDER"
GPU_BASE_URL = "FINEPDFS_GPU_BASE_URL"
GPU_MODEL = "FINEPDFS_GPU_MODEL"
GPU_API_KEY = "FINEPDFS_GPU_API_KEY"
GPU_IDENTITY = "FINEPDFS_GPU_IDENTITY"
GPU_START_COMMAND = "FINEPDFS_GPU_START_COMMAND"
GPU_STOP_COMMAND = "FINEPDFS_GPU_STOP_COMMAND"
GPU_TIMEOUT = "FINEPDFS_GPU_TIMEOUT"

NONE = "none"
STATIC = "static"
COMMAND = "command"
KNOWN_PROVIDERS = (NONE, STATIC, COMMAND)


class UnknownGpuProvider(ValueError):
    """Raised when a run names a GPU provider that does not exist, or leaves one unusable."""


class NoGpuProvider:
    """No rented GPU. The engine is reached the way it always was, from FINEPDFS_ENGINE_*."""

    @contextmanager
    def lease(self) -> Iterator[GpuEndpoint]:
        yield GpuEndpoint()


class StaticGpuProvider:
    """A GPU already running at a known address. Its address is stable, so it may name the identity."""

    def __init__(self, endpoint: GpuEndpoint) -> None:
        self._endpoint = endpoint

    @contextmanager
    def lease(self) -> Iterator[GpuEndpoint]:
        yield self._endpoint


@dataclass(frozen=True, slots=True)
class CommandGpuProvider:
    """Rents a GPU by running a start command, and returns it with a stop command.

    The start command prints one JSON object, on its own line or as the whole output:

        {"base_url": "https://...", "model": "...", "api_key": "...", "identity": "..."}

    A bare URL line is accepted too; then the model and identity are whatever the rest of the run
    configured. An identity is required for an ephemeral address, because the memo key is the identity
    (ADR-0005). When the command does not supply one it is derived from the command line and the model,
    which is stable across runs and changes when either does.
    """

    start_command: str
    stop_command: str = ""
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    @contextmanager
    def lease(self) -> Iterator[GpuEndpoint]:
        started = self.runner.run(shlex.split(self.start_command))
        require_success(started)
        endpoint = endpoint_from_start_output(started.stdout, self.start_command)
        try:
            yield endpoint
        finally:
            # The worker is given back even when the run failed: a leaked GPU bills by the hour.
            if self.stop_command:
                self.runner.run(shlex.split(self.stop_command))


def endpoint_from_start_output(stdout: str, command: str) -> GpuEndpoint:
    """Read where a start command said its worker is listening."""
    payload = _json_object(stdout)
    base_url = str(payload.get("base_url") or payload.get("url") or "").strip()
    model = str(payload.get("model") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    identity = str(payload.get("identity") or "").strip()
    if not base_url:
        base_url = _url_line(stdout)
    if not base_url:
        raise CommandFailed(f"the GPU start command printed no endpoint: {stdout.strip()[:200]!r}")
    return GpuEndpoint(
        base_url=base_url,
        model=model,
        api_key=api_key,
        identity=identity or derive_identity(command, model),
    )


def derive_identity(command: str, model: str = "") -> str:
    """A stable name for a worker whose address is not: the command line, and the model it serves."""
    digest = hashlib.sha256(command.strip().encode("utf-8")).hexdigest()[:12]
    return f"command:{digest}:{model}" if model else f"command:{digest}"


def _json_object(stdout: str) -> dict:
    """The first JSON object in the output, whole or on one line, or an empty mapping."""
    candidates = [stdout.strip(), *reversed([line.strip() for line in stdout.splitlines() if line.strip()])]
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _url_line(stdout: str) -> str:
    """The first http(s) token in the output: a command that just prints its endpoint."""
    for token in stdout.split():
        cleaned = token.strip().strip("\"'")
        if cleaned.startswith(("http://", "https://")):
            return cleaned
    return ""


def gpu_provider_from_environment(
    environ: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
) -> GpuProvider:
    """The provider a run asked for. Unset means none, which is the offline default."""
    values = environ if environ is not None else os.environ
    name = values.get(GPU_PROVIDER, "").strip().lower() or NONE
    if name == NONE:
        return NoGpuProvider()
    if name == STATIC:
        return StaticGpuProvider(_static_endpoint(values))
    if name == COMMAND:
        start_command = values.get(GPU_START_COMMAND, "").strip()
        if not start_command:
            raise UnknownGpuProvider(f"{GPU_PROVIDER}=command needs {GPU_START_COMMAND}")
        timeout = float(values.get(GPU_TIMEOUT, "1800"))
        return CommandGpuProvider(
            start_command=start_command,
            stop_command=values.get(GPU_STOP_COMMAND, "").strip(),
            runner=runner or SubprocessRunner(timeout),
        )
    raise UnknownGpuProvider(f"unknown GPU provider {name!r}; known providers are {', '.join(KNOWN_PROVIDERS)}")


def _static_endpoint(values: Mapping[str, str]) -> GpuEndpoint:
    """A fixed endpoint. Its address does not move, so the address itself may be the identity."""
    base_url = values.get(GPU_BASE_URL, "").strip() or values.get("FINEPDFS_ENGINE_BASE_URL", "").strip()
    model = values.get(GPU_MODEL, "").strip() or values.get("FINEPDFS_ENGINE_MODEL", "").strip()
    api_key = values.get(GPU_API_KEY, "").strip() or values.get("FINEPDFS_ENGINE_API_KEY", "").strip()
    identity = values.get(GPU_IDENTITY, "").strip() or values.get("FINEPDFS_ENGINE_IDENTITY", "").strip()
    if not identity and base_url:
        identity = f"static:{model}@{base_url}" if model else f"static:{base_url}"
    return GpuEndpoint(base_url=base_url, model=model, api_key=api_key, identity=identity)
