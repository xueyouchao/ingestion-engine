"""Fake Modal SDK objects for the offline worker tests.

The scripts import Modal lazily and accept the SDK as an argument, so these small stand-ins are all the
vendor surface the tests need: an app lookup, a sandbox create, a readiness wait, tunnels, terminate and
the one exception class stop classifies. No network, no GPU, no Modal package (ADR-0009).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

from deploy.gpu.contract import WORKER_MODEL, WORKER_PORT, read_lease
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
        lease_path: Path,
        tunnel_url: str | None = TUNNEL_URL,
        ready_error: Exception | None = None,
        tunnel_error: Exception | None = None,
    ) -> None:
        self.object_id = "sb-abc123"
        self.lease_path = lease_path
        self.tunnel_url = tunnel_url
        self.ready_error = ready_error
        self.tunnel_error = tunnel_error
        self.ready_timeout: int | None = None
        self.tunnel_timeout: int | None = None
        self.lease_was_recorded_before_readiness: bool | None = None
        self.terminated = False

    def wait_until_ready(self, *, timeout: int) -> None:
        self.ready_timeout = timeout
        lease = read_lease(self.lease_path)
        self.lease_was_recorded_before_readiness = lease is not None and lease.worker_id == self.object_id
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


class FakeModal:
    """Enough of the Modal module for ``start.main``."""

    def __init__(
        self,
        lease_path: Path,
        create_error: Exception | None = None,
        **sandbox_options: Any,
    ) -> None:
        self.sandbox = FakeSandbox(lease_path, **sandbox_options)
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
    exits 0 or nonzero, and the scripts see the same environment a run would give them.
    """

    def __init__(
        self,
        values: dict[str, str],
        start_modal: FakeModal,
        opener: RecordingOpener,
        stop_modal: FakeStopModal,
    ) -> None:
        self.values = values
        self.start_modal = start_modal
        self.opener = opener
        self.stop_modal = stop_modal
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Any, cwd: Path | None = None) -> CommandResult:
        self.calls.append(tuple(argv))
        if argv[0] == "start":
            output = io.StringIO()
            with redirect_stdout(output):
                code = start.main(self.values, sdk=self.start_modal, opener=self.opener)
            return CommandResult(argv=tuple(argv), returncode=code, stdout=output.getvalue())
        return CommandResult(argv=tuple(argv), returncode=stop.main(self.values, sdk=self.stop_modal))


def worker_environment(state_file: Path, **values: str) -> dict[str, str]:
    return {"FINEPDFS_GPU_STATE_FILE": str(state_file), **values}
