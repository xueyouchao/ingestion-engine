"""Fake Modal SDK objects for the offline worker tests.

The scripts import Modal lazily and accept the SDK as an argument, so these small stand-ins are all the
vendor surface the tests need: an app lookup, a sandbox create, a readiness wait, tunnels, terminate and
the one exception class stop classifies. No network, no GPU, no Modal package (ADR-0009).

Two facts about the real world are modelled rather than mocked away. Every start rents a *new* sandbox,
so the fake hands out a fresh id per start; and a sandbox's readiness wait looks for its own id in the
lease state, so the fake reads the state directory rather than assuming one file's path.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

from deploy.gpu.contract import WORKER_MODEL, WORKER_PORT, read_lease, state_directory
from deploy.modal import start, stop
from finepdfs.infrastructure.engines.subprocess_support import CommandResult

TUNNEL_URL = "https://tunnel-abc123.modal.host"


class NotFoundError(Exception):
    """The one vendor exception the tests depend on: an id the server no longer knows."""


class FakeTunnel:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeImage:
    def __init__(self) -> None:
        self.registry: tuple[str, str | None] | None = None
        self.environment: dict[str, str] = {}
        self.files: list[tuple[str, str, bool]] = []
        self.entrypoint_argv: list[str] | None = None

    def env(self, values: dict[str, str]) -> FakeImage:
        self.environment = values
        return self

    def add_local_file(self, local: Path, remote: str, *, copy: bool) -> FakeImage:
        self.files.append((str(local), remote, copy))
        return self

    def entrypoint(self, argv: list[str]) -> FakeImage:
        self.entrypoint_argv = argv
        return self


class FakeSandbox:
    def __init__(
        self,
        object_id: str,
        lease_dir: Path,
        tunnel_url: str | None = TUNNEL_URL,
        ready_error: Exception | None = None,
        tunnel_error: Exception | None = None,
    ) -> None:
        self.object_id = object_id
        self.lease_dir = lease_dir
        self.tunnel_url = tunnel_url
        self.ready_error = ready_error
        self.tunnel_error = tunnel_error
        self.ready_timeout: int | None = None
        self.tunnel_timeout: int | None = None
        self.lease_was_recorded_before_readiness: bool | None = None
        self.terminated = False

    def wait_until_ready(self, *, timeout: int) -> None:
        self.ready_timeout = timeout
        self.lease_was_recorded_before_readiness = self.object_id in self._recorded_worker_ids()
        if self.ready_error is not None:
            raise self.ready_error

    def tunnels(self, timeout: int = 50) -> dict[int, FakeTunnel]:
        self.tunnel_timeout = timeout
        if self.tunnel_error is not None:
            raise self.tunnel_error
        if self.tunnel_url is None:
            return {}
        return {WORKER_PORT: FakeTunnel(self.tunnel_url)}

    def terminate(self) -> None:
        self.terminated = True

    def _recorded_worker_ids(self) -> list[str]:
        """Every worker the state directory records, whichever lease file names it."""
        recorded = []
        for path in sorted(self.lease_dir.glob("*.json")):
            lease = read_lease(path)
            if lease is not None:
                recorded.append(lease.worker_id)
        return recorded


class FakeModal:
    """Enough of the Modal module for ``start.main``."""

    def __init__(
        self,
        lease_dir: Path,
        object_id: str = "sb-abc123",
        create_error: Exception | None = None,
        **sandbox_options: Any,
    ) -> None:
        self.sandbox = FakeSandbox(object_id, Path(lease_dir), **sandbox_options)
        self.image = FakeImage()
        self.create_error = create_error
        self.created: dict[str, Any] = {}
        self.looked_up: list[tuple[str, bool]] = []
        self.volume_names: list[tuple[str, bool]] = []
        self.registry: tuple[str, str | None] | None = None
        self.App = SimpleNamespace(lookup=self._lookup)
        self.Sandbox = SimpleNamespace(create=self._create)
        self.Probe = SimpleNamespace(with_tcp=lambda port: ("tcp-probe", port))
        self.Image = SimpleNamespace(from_registry=self._from_registry)
        self.Volume = SimpleNamespace(from_name=self._from_name)
        self.exception = SimpleNamespace(NotFoundError=NotFoundError)

    def _lookup(self, name: str, *, create_if_missing: bool) -> str:
        self.looked_up.append((name, create_if_missing))
        return "app-abc123"

    def _from_name(self, name: str, *, create_if_missing: bool) -> str:
        self.volume_names.append((name, create_if_missing))
        return f"volume-{name}"

    def _from_registry(self, tag: str, add_python: str | None = None) -> FakeImage:
        self.registry = (tag, add_python)
        return self.image

    def _create(self, **kwargs: Any) -> FakeSandbox:
        if self.create_error is not None:
            raise self.create_error
        self.created = kwargs
        return self.sandbox


class FakeStopModal:
    """Enough of the Modal module for ``stop.main``."""

    def __init__(
        self,
        from_id_error: Exception | None = None,
        terminate_error: Exception | None = None,
    ) -> None:
        self.from_id_error = from_id_error
        self.terminate_error = terminate_error
        self.from_ids: list[str] = []
        self.terminated: list[str] = []
        self.Sandbox = SimpleNamespace(from_id=self._from_id)
        self.exception = SimpleNamespace(NotFoundError=NotFoundError)

    def _from_id(self, worker_id: str) -> Any:
        self.from_ids.append(worker_id)
        if self.from_id_error is not None:
            raise self.from_id_error
        return SimpleNamespace(terminate=self._terminate)

    def _terminate(self) -> None:
        self.terminated.append(self.from_ids[-1])
        if self.terminate_error is not None:
            raise self.terminate_error


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class RecordingOpener:
    """Answers ``/v1/models`` for the start readiness check, and records what it was asked."""

    def __init__(self, model: str = WORKER_MODEL, error: Exception | None = None) -> None:
        self.model = model
        self.error = error
        self.urls: list[str] = []
        self.timeouts: list[float] = []

    def __call__(self, url: str, timeout: float) -> FakeResponse:
        self.urls.append(url)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return FakeResponse({"object": "list", "data": [{"id": self.model, "object": "model"}]})


class ScriptRunner:
    """Runs the real start/stop ``main`` functions behind the real ``CommandGpuProvider``.

    This is what proves the command contract end to end offline: the provider sees a command that
    exits 0 or nonzero, and the scripts see the environment a run would give them. The provider passes
    each lease its own identity in that environment, which is merged here exactly as a subprocess would
    inherit it. Every start rents a fresh fake sandbox and every stop is recorded, so two leases in one
    test behave like two runs against one fake Modal account.
    """

    def __init__(
        self,
        values: Mapping[str, str],
        opener: RecordingOpener,
        stop_modal: FakeStopModal,
        modal_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.values = dict(values)
        self.opener = opener
        self.stop_modal = stop_modal
        self.modal_options = dict(modal_options or {})
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str]] = []
        self.created: list[FakeSandbox] = []
        self.start_codes: list[int] = []

    def run(
        self,
        argv: Sequence[str],
        cwd: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        self.environments.append(dict(environ or {}))
        values = {**self.values, **(environ or {})}
        if argv[0] == "start":
            modal = FakeModal(
                state_directory(values),
                object_id=f"sb-{len(self.start_codes) + 1:04d}",
                **self.modal_options,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = start.main(values, sdk=modal, opener=self.opener)
            self.created.append(modal.sandbox)
            self.start_codes.append(code)
            return CommandResult(argv=tuple(argv), returncode=code, stdout=output.getvalue())
        return CommandResult(argv=tuple(argv), returncode=stop.main(values, sdk=self.stop_modal))


def worker_environment(lease_dir: Path, **values: str) -> dict[str, str]:
    return {"FINEPDFS_GPU_STATE_DIR": str(lease_dir), **values}


class FakeProcess:
    """A spawned worker process: enough to observe the start hook and the exit hook."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.waits: list[float | None] = []

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        return 0

    def kill(self) -> None:
        self.killed = True


class FakePopen:
    """Stands in for ``subprocess.Popen``: records the command, hands back one fake process."""

    def __init__(self, process: FakeProcess | None = None, error: Exception | None = None) -> None:
        self.process = process if process is not None else FakeProcess()
        self.error = error
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, command: Sequence[str], *, env: Mapping[str, str]) -> FakeProcess:
        self.commands.append(list(command))
        self.environments.append(dict(env))
        if self.error is not None:
            raise self.error
        return self.process


class FakeClock:
    """A monotonic clock a test can steer, so a readiness timeout needs no wall time."""

    def __init__(self, *times: float) -> None:
        self.times = list(times)
        self.last = times[-1] if times else 0.0

    def __call__(self) -> float:
        return self.times.pop(0) if self.times else self.last


class ModelsOpener:
    """Answers the worker's own ``/v1/models`` after N failures, and records every attempt."""

    def __init__(self, failures: int = 0, model: str = WORKER_MODEL) -> None:
        self.failures = failures
        self.model = model
        self.urls: list[str] = []
        self.timeouts: list[float] = []
        self.api_keys: list[str] = []

    def __call__(self, url: str, timeout: float, api_key: str) -> FakeResponse:
        self.urls.append(url)
        self.timeouts.append(timeout)
        self.api_keys.append(api_key)
        if self.failures:
            self.failures -= 1
            raise ConnectionError("the worker is still loading")
        return FakeResponse({"object": "list", "data": [{"id": self.model, "object": "model"}]})


class FakeServerRegistration:
    """What ``@app.server(...)`` hands back: the decorated class and every pin it was given."""

    def __init__(self, user_cls: type, options: Mapping[str, Any]) -> None:
        self.user_cls = user_cls
        self.options = dict(options)


class FakeSecret:
    def __init__(self, name: str, required_keys: Sequence[str]) -> None:
        self.name = name
        self.required_keys = list(required_keys)


class FakeDeployApp:
    """One fake Modal App: records the decorators and the server class that carries them."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.servers: dict[str, FakeServerRegistration] = {}

    def server(self, **options: Any) -> Any:
        def register(user_cls: type) -> FakeServerRegistration:
            registration = FakeServerRegistration(user_cls, options)
            self.servers[options.get("name", "")] = registration
            return registration

        return register


class FakeDeployModal:
    """A module-like stand-in for ``import modal``: the deployed app's whole SDK surface."""

    def __init__(self) -> None:
        self.apps: list[FakeDeployApp] = []
        self.image = FakeImage()
        self.volumes: list[tuple[str, bool]] = []
        self.secrets: list[FakeSecret] = []
        self.enters: list[str] = []
        self.exits: list[str] = []
        self.App = self._app
        self.Image = SimpleNamespace(from_registry=self._from_registry)
        self.Volume = SimpleNamespace(from_name=self._from_name)
        self.Secret = SimpleNamespace(from_name=self._secret)
        self.enter = self._enter
        self.exit = self._exit
        self.exception = SimpleNamespace(NotFoundError=NotFoundError)

    def _app(self, name: str) -> FakeDeployApp:
        app = FakeDeployApp(name)
        self.apps.append(app)
        return app

    def _from_registry(self, tag: str, add_python: str | None = None) -> FakeImage:
        return self.image

    def _from_name(self, name: str, *, create_if_missing: bool) -> str:
        self.volumes.append((name, create_if_missing))
        return f"volume-{name}"

    def _secret(self, name: str, *, required_keys: Sequence[str] = ()) -> FakeSecret:
        secret = FakeSecret(name, required_keys)
        self.secrets.append(secret)
        return secret

    def _enter(self) -> Any:
        def decorate(function: Any) -> Any:
            self.enters.append(getattr(function, "__name__", ""))
            function.modal_enter = True
            return function

        return decorate

    def _exit(self) -> Any:
        def decorate(function: Any) -> Any:
            self.exits.append(getattr(function, "__name__", ""))
            function.modal_exit = True
            return function

        return decorate


class FakeAutoscalerSettings:
    def __init__(self, min_containers: int | None, scaledown_window: int | None) -> None:
        self.min_containers = min_containers
        self.max_containers = 1
        self.target_concurrency = None
        self.buffer_containers = 0
        self.scaleup_window = None
        self.scaledown_window = scaledown_window


class FakeWarmServer:
    """One deployed Server as the Warm Control sees it: a URL and an autoscaler behind one call."""

    def __init__(self, url: str | None, floor: int, scaledown_window: int) -> None:
        self.url = url
        self.floor = floor
        self.scaledown_window = scaledown_window
        self.updates: list[dict[str, int]] = []

    def get_url(self) -> str | None:
        return self.url

    def update_autoscaler(self, **options: int) -> FakeAutoscalerSettings:
        self.updates.append(dict(options))
        if "min_containers" in options:
            self.floor = options["min_containers"]
        return FakeAutoscalerSettings(self.floor, self.scaledown_window)


class FakeWarmModal:
    """A module-like stand-in for ``import modal`` on the Warm Control's path."""

    def __init__(
        self,
        url: str | None = TUNNEL_URL,
        floor: int = 0,
        scaledown_window: int = 180,
    ) -> None:
        self.server = FakeWarmServer(url, floor, scaledown_window)
        self.looked_up: list[tuple[str, str]] = []
        self.Server = SimpleNamespace(from_name=self._from_name)
        self.exception = SimpleNamespace(NotFoundError=NotFoundError)

    def _from_name(self, app_name: str, server_name: str) -> FakeWarmServer:
        self.looked_up.append((app_name, server_name))
        return self.server
