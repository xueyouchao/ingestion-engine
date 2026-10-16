"""Renting a GPU: the endpoint a provider hands back, and the memo key it must not disturb.

The interesting test here is the memo one. A rented worker answers on a new host every run, and the
memo key is the engine identity (ADR-0005). If the address leaked into that key, every run would read
the whole corpus again -- which is the cost the memo exists to remove.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument
from finepdfs.infrastructure.cocoindex.runtime import runtime_from_environment
from finepdfs.infrastructure.compute import providers
from finepdfs.infrastructure.compute.endpoint import GpuEndpoint, export_endpoint
from finepdfs.infrastructure.compute.providers import (
    CommandGpuProvider,
    NoGpuProvider,
    StaticGpuProvider,
    UnknownGpuProvider,
    derive_identity,
    endpoint_from_start_output,
    gpu_provider_from_environment,
)
from finepdfs.infrastructure.engines.registry import EngineConfig, build_engine
from finepdfs.infrastructure.engines.stable_identity import StableIdentity
from finepdfs.infrastructure.engines.subprocess_support import CommandFailed, CommandResult
from finepdfs.interfaces.cli import build_parser, main

START = "start-worker"
STOP = "stop-worker"


class RecordingRunner:
    """A runner that records what it was asked to run, and answers with a scripted stdout."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], cwd: Path | None = None) -> CommandResult:
        self.calls.append(tuple(argv))
        stdout = self.stdout if argv[0] == START else ""
        return CommandResult(argv=tuple(argv), returncode=self.returncode, stdout=stdout)


class CountingEngine:
    """An engine that reports the address it was reached at, and counts extractions."""

    def __init__(self, name: str = "fake", detail: str = "model@https://ephemeral.example/v1") -> None:
        self._identity = EngineIdentity(name=name, detail=detail)
        self.extracted: list[Document] = []

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document) -> ExtractedDocument:
        self.extracted.append(document)
        return ExtractedDocument(pages=(), engine=self._identity)


def test_without_a_provider_nothing_is_rented() -> None:
    provider = gpu_provider_from_environment({})

    assert isinstance(provider, NoGpuProvider)
    with provider.lease() as endpoint:
        assert endpoint == GpuEndpoint()


def test_a_static_provider_names_the_model_not_the_address() -> None:
    """ADR-0007: address and identity are separate fields, so the model alone names the deployment."""
    provider = gpu_provider_from_environment(
        {"FINEPDFS_GPU_PROVIDER": "static", "FINEPDFS_GPU_BASE_URL": "http://host:8000/v1", "FINEPDFS_GPU_MODEL": "m"}
    )

    assert isinstance(provider, StaticGpuProvider)
    with provider.lease() as endpoint:
        assert endpoint.base_url == "http://host:8000/v1"
        assert endpoint.identity == "static:m"


def test_a_static_provider_without_a_model_is_still_named_static() -> None:
    provider = gpu_provider_from_environment(
        {"FINEPDFS_GPU_PROVIDER": "static", "FINEPDFS_GPU_BASE_URL": "http://host:8000/v1"}
    )

    with provider.lease() as endpoint:
        assert endpoint.identity == "static"


def test_moving_a_static_address_does_not_move_the_memo_key() -> None:
    """The provider's identity rule, checked where it becomes the memo key (ADR-0005, ADR-0007)."""

    def memo_key(base_url: str) -> str:
        environ = {
            "FINEPDFS_GPU_PROVIDER": "static",
            "FINEPDFS_GPU_BASE_URL": base_url,
            "FINEPDFS_GPU_MODEL": "PaddleOCR-VL-1.6",
            "FINEPDFS_ENGINE": "paddleocr-vl",
        }
        with gpu_provider_from_environment(environ).lease() as endpoint:
            export_endpoint(endpoint, environ)
        return str(runtime_from_environment(environ).engines["paddleocr-vl"].identity)

    assert memo_key("http://a.example:8000/v1") == memo_key("http://b.example:8000/v1")


def test_a_command_provider_parses_the_endpoint_and_gives_it_back() -> None:
    runner = RecordingRunner(
        stdout='{"base_url": "https://rented.example/v1", "model": "PaddleOCR-VL-1.6", "api_key": "tok"}'
    )
    provider = CommandGpuProvider(start_command=START, stop_command=STOP, runner=runner)

    with provider.lease() as endpoint:
        assert endpoint.base_url == "https://rented.example/v1"
        assert endpoint.model == "PaddleOCR-VL-1.6"
        assert endpoint.api_key == "tok"

    assert [call[0] for call in runner.calls] == [START, STOP]


def test_a_command_provider_gives_the_worker_back_even_when_the_run_failed() -> None:
    """A leaked GPU bills by the hour, so the stop command runs on the way out, not only on success."""
    runner = RecordingRunner(stdout="https://rented.example/v1")
    provider = CommandGpuProvider(start_command=START, stop_command=STOP, runner=runner)

    with pytest.raises(RuntimeError), provider.lease():
        raise RuntimeError("the run blew up")

    assert [call[0] for call in runner.calls] == [START, STOP]


def test_the_endpoint_is_the_first_json_object_not_a_later_log_line() -> None:
    """A start command that prints its endpoint and then logs JSON must yield the endpoint, not the log."""
    stdout = (
        '{"base_url": "https://rented.example/v1", "identity": "worker"}\n'
        '{"base_url": "https://dashboard.example", "level": "info"}\n'
    )

    endpoint = endpoint_from_start_output(stdout, START)

    assert endpoint.base_url == "https://rented.example/v1"
    assert endpoint.identity == "worker"


def test_a_command_that_only_prints_a_url_is_understood() -> None:
    endpoint = endpoint_from_start_output("booting...\nhttps://rented.example/v1\nready\n", START)

    assert endpoint.base_url == "https://rented.example/v1"
    assert endpoint.identity.startswith("command:")


def test_a_derived_identity_is_stable_across_runs_and_follows_the_model() -> None:
    assert derive_identity(START, "m") == derive_identity(START, "m")
    assert derive_identity(START, "m") != derive_identity(START, "other")
    assert derive_identity(START, "m") != derive_identity("another-command", "m")


def test_a_start_command_that_prints_nothing_is_an_error() -> None:
    with pytest.raises(CommandFailed, match="printed no endpoint"):
        endpoint_from_start_output("nothing useful here", START)


def test_a_command_provider_without_a_start_command_is_rejected() -> None:
    with pytest.raises(UnknownGpuProvider):
        gpu_provider_from_environment({"FINEPDFS_GPU_PROVIDER": "command"})


def test_a_new_provider_is_registered_in_one_place(monkeypatch) -> None:
    """A Modal or RunPod adapter should be one entry the factory and the CLI both read."""
    class ModalProvider:
        @contextmanager
        def lease(self):
            yield GpuEndpoint(base_url="https://modal.example/v1", identity="modal:ocr")

    monkeypatch.setitem(providers.PROVIDERS, "modal", lambda values, runner: ModalProvider())

    provider = gpu_provider_from_environment({"FINEPDFS_GPU_PROVIDER": "modal"})

    assert isinstance(provider, ModalProvider)
    assert build_parser().parse_args(["dry-run", "--gpu-provider", "modal"]).gpu_provider == "modal"


@pytest.mark.parametrize("name", sorted(providers.PROVIDERS))
def test_every_registered_provider_is_also_a_cli_choice(name: str) -> None:
    args = build_parser().parse_args(["dry-run", "--gpu-provider", name])

    assert args.gpu_provider == name


def test_an_unknown_provider_is_rejected() -> None:
    with pytest.raises(UnknownGpuProvider):
        gpu_provider_from_environment({"FINEPDFS_GPU_PROVIDER": "magic-cloud"})


def test_exporting_an_endpoint_moves_the_address_but_not_a_pinned_identity() -> None:
    environ = {"FINEPDFS_ENGINE_IDENTITY": "pinned:v1"}

    export_endpoint(
        GpuEndpoint(base_url="https://new.example/v1", model="m", api_key="k", identity="provider-derived"),
        environ,
    )

    assert environ["FINEPDFS_ENGINE_BASE_URL"] == "https://new.example/v1"
    assert environ["FINEPDFS_ENGINE_MODEL"] == "m"
    assert environ["FINEPDFS_ENGINE_API_KEY"] == "k"
    assert environ["FINEPDFS_ENGINE_IDENTITY"] == "pinned:v1"


def test_an_empty_endpoint_leaves_the_environment_alone() -> None:
    environ = {"FINEPDFS_ENGINE_BASE_URL": "http://kept:8000/v1"}

    export_endpoint(GpuEndpoint(), environ)

    assert environ == {"FINEPDFS_ENGINE_BASE_URL": "http://kept:8000/v1"}


def test_a_moving_address_does_not_move_the_memo_key() -> None:
    """The whole point of the layer: yesterday's worker and today's worker are the same worker."""
    first = build_engine(
        EngineConfig(name="paddleocr-vl", base_url="https://a.example/v1", model="m", detail="modal:ocr:1.6")
    )
    second = build_engine(
        EngineConfig(name="paddleocr-vl", base_url="https://b.example/v1", model="m", detail="modal:ocr:1.6")
    )

    assert str(first.identity) == str(second.identity) == "paddleocr-vl@modal:ocr:1.6"


def test_without_an_identity_the_address_is_the_key() -> None:
    """The hazard the layer removes: this is what would re-read the corpus on every run."""
    first = build_engine(EngineConfig(name="paddleocr-vl", base_url="https://a.example/v1", model="m"))
    second = build_engine(EngineConfig(name="paddleocr-vl", base_url="https://b.example/v1", model="m"))

    assert str(first.identity) != str(second.identity)


def test_a_stable_identity_keeps_the_engine_name_and_stamps_its_own_identity() -> None:
    inner = CountingEngine(name="paddleocr-vl")
    engine = StableIdentity(inner, "modal:ocr:1.6")
    document = Document(name="doc", content=b"bytes")

    result = engine.extract(document)

    assert engine.identity == EngineIdentity(name="paddleocr-vl", detail="modal:ocr:1.6")
    assert inner.extracted == [document]
    assert result.engine == engine.identity


def test_the_runtime_builds_the_engine_from_the_exported_identity() -> None:
    """A lease reaches the flow only through the environment, so that path has to be wired."""
    runtime = runtime_from_environment(
        {
            "FINEPDFS_ENGINE": "paddleocr-vl",
            "FINEPDFS_ENGINE_BASE_URL": "https://rented.example/v1",
            "FINEPDFS_ENGINE_MODEL": "m",
            "FINEPDFS_ENGINE_IDENTITY": "modal:ocr:1.6",
        }
    )

    assert str(runtime.engines["paddleocr-vl"].identity) == "paddleocr-vl@modal:ocr:1.6"


def test_a_misconfigured_provider_fails_the_cli_cleanly(monkeypatch, capsys) -> None:
    """A run that cannot start is an operator problem: a message and exit 2, not a traceback."""
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.delenv("FINEPDFS_GPU_START_COMMAND", raising=False)

    code = main(["dry-run", "--engine", "paddleocr-vl"])

    assert code == 2
    assert "FINEPDFS_GPU_START_COMMAND" in capsys.readouterr().err
