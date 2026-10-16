"""The GPU providers a run can choose from: no rented GPU, a fixed server, or one a command rents.

The last one is the seam that lets a Modal or RunPod worker be injected without this package knowing
anything about either: the start command prints where its worker is listening, the run uses it, and the
stop command gives it back. Provider SDKs, when they are wanted, are just another command or another
adapter behind the same two methods.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import sys
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from finepdfs.infrastructure.compute.endpoint import (
    ENGINE_API_KEY,
    ENGINE_BASE_URL,
    ENGINE_IDENTITY,
    ENGINE_MODEL,
    GpuEndpoint,
    GpuProvider,
    export_endpoint,
)
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

logger = logging.getLogger(__name__)


class UnknownGpuProvider(ValueError):
    """Raised when a run names a GPU provider that does not exist."""


class MisconfiguredGpuProvider(ValueError):
    """Raised when a known GPU provider is missing what it needs to run, or what it was given is unusable.

    A class of its own, not ``UnknownGpuProvider``: "there is no provider by that name" and "the
    provider you named cannot run" are different failures with different fixes, and the CLI decides by
    type. The unknown error stays for an actually unknown name. A command with unbalanced quotes is the
    same kind of fault as a missing one: the provider cannot do its job until the operator fixes what
    they typed.
    """


class GpuStartFailed(RuntimeError):
    """Raised when a provider cannot start its worker, so the run cannot lease an endpoint.

    A class of its own, not ``CommandFailed``: an engine raises that too when its own command fails, and
    a run that cannot start is an operator problem the CLI reports, while an engine that started and
    then failed keeps its traceback (ADR-0007).
    """


class GpuStopFailed(RuntimeError):
    """Raised when a provider cannot give its worker back, so the worker may still be running.

    A class of its own, not ``GpuStartFailed``: "the worker never started" and "the worker is still out
    there, billing" are different faults with different fixes, and an error names the failure that
    occurred (CODING_STANDARDS). A leased GPU bills by the hour, which is why ADR-0007 exists; a stop
    that fails after an otherwise good run is a provider failure the CLI reports and exits 2 on.
    """


class NoGpuProvider:
    """No rented GPU. The engine is reached the way it always was, from FINEPDFS_ENGINE_*."""

    @contextmanager
    def lease(self) -> Iterator[GpuEndpoint]:
        yield GpuEndpoint()


class StaticGpuProvider:
    """A GPU already running at a known address.

    The address is where the bytes go, not who the worker is: even a fixed server's address belongs
    in ``base_url`` alone, so a memoised corpus survives the server being moved (ADR-0007).
    """

    def __init__(self, endpoint: GpuEndpoint) -> None:
        self._endpoint = endpoint

    @contextmanager
    def lease(self) -> Iterator[GpuEndpoint]:
        yield self._endpoint


def _argv(command: str) -> list[str]:
    """Split one of the provider's commands, naming an unparseable one as a provider fault.

    ``shlex.split`` raises ``ValueError`` on unbalanced quotes. That is neither a provider error the CLI
    reports nor an engine failure, so left alone it prints a traceback where ADR-0007 promises the exit-2
    message for a provider that cannot start the run. The command the operator typed comes with it, so
    the message points at the typo rather than at shlex.
    """
    try:
        return shlex.split(command)
    except ValueError as error:
        raise MisconfiguredGpuProvider(f"the GPU command is malformed: {command!r} ({error})") from error


@dataclass(frozen=True, slots=True)
class CommandGpuProvider:
    """Rents a GPU by running a start command, and returns it with a stop command.

    The start command prints one JSON object, on its own line or as the whole output:

        {"base_url": "https://...", "model": "...", "api_key": "...", "identity": "..."}

    A bare URL line is accepted too. Then the command names no model, so the model the run configured
    is the one in play, and the derived identity names it (ADR-0007). An identity is required for an
    ephemeral address, because the memo key is the identity (ADR-0005). When the command does not
    supply one it is derived from the command line and the model, which is stable across runs and
    changes when either does.

    The stop command's result is checked rather than discarded: a worker it could not give back is
    still billing, which is the cost ADR-0007 exists to remove.
    """

    start_command: str
    stop_command: str = ""
    model: str = ""
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    @contextmanager
    def lease(self) -> Iterator[GpuEndpoint]:
        # The start command is the provider's own, so its failure is "this run cannot start", not an
        # engine failure: wrap it in the error the CLI reports, and keep CommandFailed for engines.
        try:
            started = self.runner.run(_argv(self.start_command))
            require_success(started)
        except CommandFailed as error:
            raise GpuStartFailed(f"the GPU start command failed: {error}") from error
        endpoint = endpoint_from_start_output(started.stdout, self.start_command, self.model)
        try:
            yield endpoint
        finally:
            # The worker is given back however the run ended: a leaked GPU bills by the hour. The error
            # the run ended on is the one in flight right here, whether it was thrown into this lease by
            # a plain `with`, or -- under CocoIndex -- never thrown in at all: the lifespan generator is
            # resumed with `next()` while the failed update is still unwinding, so the `yield` above sees
            # no exception to catch and the release has to read the traceback in flight instead.
            self._release_reporting(sys.exc_info()[1])

    def _release_reporting(self, run_error: BaseException | None) -> None:
        """Give the worker back, and report a stop that failed without hiding the run's own error.

        A stop that fails after a run that otherwise finished is the provider's failure, so
        ``GpuStopFailed`` is what the CLI reports and exits 2 on. A stop that fails while the run is
        already failing is reported beside that failure -- logged, and attached to its traceback as a
        note -- and the run's own error is left to propagate untouched, because that is the error the
        operator has to act on (ADR-0007).
        """
        try:
            self._release()
        except Exception as leak:
            if run_error is None:
                raise
            logger.error(
                "the GPU stop command failed after the run had already failed, so the worker may "
                "still be billing: %s",
                leak,
            )
            run_error.add_note(f"the GPU stop command also failed: {leak}")

    def _release(self) -> None:
        """Run the stop command, and refuse to pass a failure over in silence.

        The command is the provider's own, so a failure here is the provider's fault and not an engine's,
        and it means the worker is still up: the one thing ADR-0007 exists to prevent. The runner's own
        failure is wrapped with the exit code, because ``SubprocessRunner`` raises ``CommandFailed`` for
        a binary it cannot find before there is any exit code to read.
        """
        if not self.stop_command:
            return
        try:
            require_success(self.runner.run(_argv(self.stop_command)))
        except CommandFailed as error:
            raise GpuStopFailed(f"the GPU stop command failed: {error}") from error


def endpoint_from_start_output(stdout: str, command: str, model: str = "") -> GpuEndpoint:
    """Read where a start command said its worker is listening.

    ``model`` is the model the run configured for the engine. It is the model in play when the command
    prints a bare URL and therefore names none of its own, so the derived identity can still name it:
    ADR-0007 derives the identity from the command line *and the model*.

    A command that names no endpoint cannot start the run, so this function -- the provider's, not an
    engine's -- raises the provider's own ``GpuStartFailed``, which is what the CLI reports and exits 2
    on, rather than ``CommandFailed``, which stays an engine's failure.
    """
    payload = _json_object(stdout)
    base_url = str(payload.get("base_url") or payload.get("url") or "").strip()
    served_model = str(payload.get("model") or "").strip() or model.strip()
    api_key = str(payload.get("api_key") or "").strip()
    identity = str(payload.get("identity") or "").strip()
    if not base_url:
        base_url = _url_line(stdout)
    if not base_url:
        raise GpuStartFailed(f"the GPU start command printed no endpoint: {stdout.strip()[:200]!r}")
    return GpuEndpoint(
        base_url=base_url,
        model=served_model,
        api_key=api_key,
        identity=identity or derive_identity(command, served_model),
    )


def derive_identity(command: str, model: str = "") -> str:
    """A stable name for a worker whose address is not: the command line, and the model it serves."""
    digest = hashlib.sha256(command.strip().encode("utf-8")).hexdigest()[:12]
    return f"command:{digest}:{model}" if model else f"command:{digest}"


def _json_object(stdout: str) -> dict:
    """The first JSON object in the output, whole or on one line, or an empty mapping.

    First, not last: a start command may log JSON after it has named its endpoint, and a later log
    line must not be mistaken for the endpoint. The whole output is tried first so a command that
    prints its endpoint as one pretty-printed object is understood even though it spans lines.
    """
    candidates = [stdout.strip(), *[line.strip() for line in stdout.splitlines() if line.strip()]]
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


ProviderFactory = Callable[[Mapping[str, str], CommandRunner | None], GpuProvider]


def _none_provider(_values: Mapping[str, str], _runner: CommandRunner | None) -> GpuProvider:
    """No rented GPU: the engine is reached the way it always was, from FINEPDFS_ENGINE_*."""
    return NoGpuProvider()


def _static_provider(values: Mapping[str, str], _runner: CommandRunner | None) -> GpuProvider:
    return StaticGpuProvider(_static_endpoint(values))


def _command_provider(values: Mapping[str, str], runner: CommandRunner | None) -> GpuProvider:
    start_command = values.get(GPU_START_COMMAND, "").strip()
    if not start_command:
        raise MisconfiguredGpuProvider(f"{GPU_PROVIDER}={COMMAND} needs {GPU_START_COMMAND}")
    timeout = float(values.get(GPU_TIMEOUT, "1800"))
    return CommandGpuProvider(
        start_command=start_command,
        stop_command=values.get(GPU_STOP_COMMAND, "").strip(),
        # FINEPDFS_ENGINE_MODEL is the model the run will actually ask for: it is the one the engine is
        # built with, and the one a bare URL's derived identity has to name. GPU_MODEL belongs to the
        # static provider and never reaches the engine, so it is not a fallback here.
        model=values.get(ENGINE_MODEL, "").strip(),
        runner=runner or SubprocessRunner(timeout),
    )


# The one registration point: a new adapter adds its name here, and the factory and the CLI both read
# this mapping rather than repeating the list. No entry points, no plugin framework: one dict.
PROVIDERS: dict[str, ProviderFactory] = {
    NONE: _none_provider,
    STATIC: _static_provider,
    COMMAND: _command_provider,
}


def gpu_provider_from_environment(
    environ: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
) -> GpuProvider:
    """The provider a run asked for. Unset means none, which is the offline default."""
    values = environ if environ is not None else os.environ
    name = values.get(GPU_PROVIDER, "").strip().lower() or NONE
    factory = PROVIDERS.get(name)
    if factory is None:
        raise UnknownGpuProvider(
            f"unknown GPU provider {name!r}; known providers are {', '.join(PROVIDERS)}"
        )
    return factory(values, runner)


_ENGINE_VARIABLES = (ENGINE_BASE_URL, ENGINE_MODEL, ENGINE_API_KEY, ENGINE_IDENTITY)


@contextmanager
def leased_environment(environ: MutableMapping[str, str] | None = None) -> Iterator[GpuEndpoint]:
    """Lease the GPU this run asked for, then publish it as the environment the flow reads.

    Every entry point is wired the same way -- lease, export, build -- so that sequence has one home
    rather than one copy per caller. The environment is restored to exactly what it held before the
    lease ends, whether the run succeeded or failed: a worker that has been given back must not stay
    in the environment as an engine a later in-process run would silently target.
    """
    target = environ if environ is not None else os.environ
    previous = {name: target[name] for name in _ENGINE_VARIABLES if name in target}
    try:
        with gpu_provider_from_environment(target).lease() as endpoint:
            export_endpoint(endpoint, target)
            yield endpoint
    finally:
        for name in _ENGINE_VARIABLES:
            if name in previous:
                target[name] = previous[name]
            else:
                target.pop(name, None)


def _static_endpoint(values: Mapping[str, str]) -> GpuEndpoint:
    """A fixed endpoint named by its model, never by its address.

    ``FINEPDFS_ENGINE_IDENTITY`` and ``FINEPDFS_GPU_IDENTITY`` win when set; otherwise the model names
    the deployment, which is what keeps the memo key still when the address moves (ADR-0007). An
    operator who wants a different name has the identity variables for it.
    """
    base_url = values.get(GPU_BASE_URL, "").strip() or values.get(ENGINE_BASE_URL, "").strip()
    model = values.get(GPU_MODEL, "").strip() or values.get(ENGINE_MODEL, "").strip()
    api_key = values.get(GPU_API_KEY, "").strip() or values.get(ENGINE_API_KEY, "").strip()
    identity = values.get(GPU_IDENTITY, "").strip() or values.get(ENGINE_IDENTITY, "").strip()
    if not identity:
        identity = f"static:{model}" if model else STATIC
    return GpuEndpoint(base_url=base_url, model=model, api_key=api_key, identity=identity)
