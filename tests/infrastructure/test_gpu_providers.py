"""Renting a GPU: the endpoint a provider hands back, and the memo key it must not disturb.

The interesting test here is the memo one. A rented worker answers on a new host every run, and the
memo key is the engine identity (ADR-0005). If the address leaked into that key, every run would read
the whole corpus again -- which is the cost the memo exists to remove.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument
from finepdfs.infrastructure.cocoindex.runtime import runtime_from_environment
from finepdfs.infrastructure.compute import providers
from finepdfs.infrastructure.compute.endpoint import (
    ENGINE_BASE_URL,
    ENGINE_DETAIL,
    GpuEndpoint,
    export_endpoint,
)
from finepdfs.infrastructure.compute.providers import (
    GPU_PROVIDER,
    GPU_START_COMMAND,
    GPU_STOP_COMMAND,
    CommandGpuProvider,
    GpuStartFailed,
    GpuStopFailed,
    MisconfiguredGpuProvider,
    NoGpuProvider,
    StaticGpuProvider,
    UnknownGpuProvider,
    derive_detail,
    endpoint_from_start_output,
    gpu_provider_from_environment,
    leased_environment,
)
from finepdfs.infrastructure.engines.registry import EngineConfig, build_engine
from finepdfs.infrastructure.engines.stable_identity import StableIdentity
from finepdfs.infrastructure.engines.subprocess_support import CommandFailed, CommandResult
from finepdfs.interfaces.cli import ENVIRONMENT_FOR_ENGINE, build_parser, main

START = "start-worker"
STOP = "stop-worker"


class RecordingRunner:
    """A runner that records what it was asked to run, and answers with a scripted result.

    The start and stop commands fail separately, because the two failures are not the same failure: a
    start that fails means no run, a stop that fails means a worker somebody is still paying for.
    """

    def __init__(
        self, stdout: str = "", returncode: int = 0, stop_returncode: int = 0, stop_error: Exception | None = None
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stop_returncode = stop_returncode
        self.stop_error = stop_error
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], cwd: Path | None = None) -> CommandResult:
        self.calls.append(tuple(argv))
        starting = argv[0] == START
        if starting:
            return CommandResult(argv=tuple(argv), returncode=self.returncode, stdout=self.stdout)
        if self.stop_error is not None:
            raise self.stop_error
        return CommandResult(argv=tuple(argv), returncode=self.stop_returncode)


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
    """ADR-0007: address and detail are separate fields, so the model alone names the deployment."""
    provider = gpu_provider_from_environment(
        {"FINEPDFS_GPU_PROVIDER": "static", "FINEPDFS_GPU_BASE_URL": "http://host:8000/v1", "FINEPDFS_GPU_MODEL": "m"}
    )

    assert isinstance(provider, StaticGpuProvider)
    with provider.lease() as endpoint:
        assert endpoint.base_url == "http://host:8000/v1"
        assert endpoint.detail == "static:m"


def test_a_static_provider_without_a_model_is_still_named_static() -> None:
    provider = gpu_provider_from_environment(
        {"FINEPDFS_GPU_PROVIDER": "static", "FINEPDFS_GPU_BASE_URL": "http://host:8000/v1"}
    )

    with provider.lease() as endpoint:
        assert endpoint.detail == "static"


def test_the_pinned_engine_detail_wins_the_static_providers_own_name() -> None:
    """The two names meet in one place, so one rule decides -- and it must be the rule the README states.

    ADR-0007 keeps an identity that is already set, so ``--engine-detail`` pins the memo name and
    outlives the provider's default. The static provider's own ``FINEPDFS_GPU_DETAIL`` is that
    default, so a pinned ``FINEPDFS_ENGINE_DETAIL`` wins it; ``export_endpoint``'s setdefault then
    agrees with the one decision instead of quietly taking it back.
    """
    environ = {
        "FINEPDFS_GPU_PROVIDER": "static",
        "FINEPDFS_GPU_BASE_URL": "http://host:8000/v1",
        "FINEPDFS_GPU_MODEL": "m",
        "FINEPDFS_GPU_DETAIL": "gpu:v1",
        "FINEPDFS_ENGINE_DETAIL": "pinned:v1",
    }

    with leased_environment(environ) as endpoint:
        assert endpoint.detail == "pinned:v1", "the pinned detail is the memo name"
        assert environ["FINEPDFS_ENGINE_DETAIL"] == "pinned:v1", "the export must not decide it again"


def test_the_gpu_detail_names_the_static_deployment_when_nothing_is_pinned() -> None:
    """The provider's own name is the fallback, not a loser: with no pin it is what the memo remembers."""
    environ = {
        "FINEPDFS_GPU_PROVIDER": "static",
        "FINEPDFS_GPU_BASE_URL": "http://host:8000/v1",
        "FINEPDFS_GPU_MODEL": "m",
        "FINEPDFS_GPU_DETAIL": "gpu:v1",
    }

    with leased_environment(environ) as endpoint:
        assert endpoint.detail == "gpu:v1"
        assert environ["FINEPDFS_ENGINE_DETAIL"] == "gpu:v1"


def test_an_empty_pinned_detail_does_not_drop_the_providers_own_name() -> None:
    """An empty FINEPDFS_ENGINE_DETAIL is the unset value, not a pin, so the provider's name still wins.

    The static provider decided the detail and then ``export_endpoint``'s ``setdefault`` kept the
    present-but-empty key, so ``build_engine`` saw ``detail=""``, skipped ``StableIdentity``, and the
    memo key fell back to the address ADR-0007 keeps out of it. Both readers now agree that empty is
    unset.
    """
    environ = {
        "FINEPDFS_GPU_PROVIDER": "static",
        "FINEPDFS_GPU_BASE_URL": "http://host:8000/v1",
        "FINEPDFS_GPU_MODEL": "m",
        "FINEPDFS_GPU_DETAIL": "gpu:v1",
        "FINEPDFS_ENGINE_DETAIL": "",
    }

    with leased_environment(environ) as endpoint:
        assert endpoint.detail == "gpu:v1"
        assert environ["FINEPDFS_ENGINE_DETAIL"] == "gpu:v1"


def test_an_empty_pinned_detail_still_lets_the_provider_name_the_memo_key() -> None:
    """The hazard at its sharpest: a rented address must not reach the memo key because a key was empty."""
    runner = RecordingRunner(stdout="https://rented.example/v1")
    environ = {
        "FINEPDFS_GPU_PROVIDER": "command",
        "FINEPDFS_GPU_START_COMMAND": START,
        "FINEPDFS_GPU_STOP_COMMAND": STOP,
        "FINEPDFS_ENGINE": "paddleocr-vl",
        "FINEPDFS_ENGINE_MODEL": "m",
        "FINEPDFS_ENGINE_DETAIL": "",
    }

    with gpu_provider_from_environment(environ, runner=runner).lease() as endpoint:
        export_endpoint(endpoint, environ)
        key = str(runtime_from_environment(environ).engines["paddleocr-vl"].identity)

    assert endpoint.detail.startswith("command:")
    assert environ["FINEPDFS_ENGINE_DETAIL"] == endpoint.detail
    assert key == f"paddleocr-vl@{endpoint.detail}@dpi200"


def test_moving_a_static_address_does_not_move_the_memo_key() -> None:
    """The provider's detail rule, checked where it becomes the memo key (ADR-0005, ADR-0007)."""

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


def test_a_stop_command_that_fails_is_not_discarded() -> None:
    """A stop that exits nonzero leaves the worker up and billing: that is not a result to throw away."""
    runner = RecordingRunner(stdout="https://rented.example/v1", stop_returncode=1)
    provider = CommandGpuProvider(start_command=START, stop_command=STOP, runner=runner)

    with pytest.raises(GpuStopFailed, match=STOP), provider.lease():
        pass

    assert [call[0] for call in runner.calls] == [START, STOP], "the stop was attempted before it failed"
    assert not issubclass(GpuStopFailed, GpuStartFailed), "the two faults send a reader to different places"


def test_a_failing_stop_does_not_mask_the_error_that_ended_the_run(caplog) -> None:
    """The run's own error is what the operator acts on; the leak is reported beside it, not in place."""
    runner = RecordingRunner(stdout="https://rented.example/v1", stop_returncode=1)
    provider = CommandGpuProvider(start_command=START, stop_command=STOP, runner=runner)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="the run blew up"), provider.lease():
        raise RuntimeError("the run blew up")

    assert [call[0] for call in runner.calls] == [START, STOP]
    assert "billing" in caplog.text, "the leak is reported even though it must not replace the run's error"


def test_a_stop_that_explodes_does_not_mask_the_error_that_ended_the_run(caplog) -> None:
    """Any stop failure masks the run's error if it escapes, not only the types the provider wraps."""
    runner = RecordingRunner(
        stdout="https://rented.example/v1", stop_error=OSError("the control plane is unreachable")
    )
    provider = CommandGpuProvider(start_command=START, stop_command=STOP, runner=runner)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="the run blew up"), provider.lease():
        raise RuntimeError("the run blew up")

    assert "billing" in caplog.text
    assert "control plane" in caplog.text, "the message says what the stop attempt actually did"


def test_the_endpoint_is_the_first_json_object_not_a_later_log_line() -> None:
    """A start command that prints its endpoint and then logs JSON must yield the endpoint, not the log."""
    stdout = (
        '{"base_url": "https://rented.example/v1", "detail": "worker"}\n'
        '{"base_url": "https://dashboard.example", "level": "info"}\n'
    )

    endpoint = endpoint_from_start_output(stdout, START)

    assert endpoint.base_url == "https://rented.example/v1"
    assert endpoint.detail == "worker"


def test_a_command_that_only_prints_a_url_is_understood() -> None:
    endpoint = endpoint_from_start_output("booting...\nhttps://rented.example/v1\nready\n", START)

    assert endpoint.base_url == "https://rented.example/v1"
    assert endpoint.detail.startswith("command:")


def test_a_bare_url_still_names_the_model_the_run_configured() -> None:
    """ADR-0007: the derived detail is the command line *and the model*, so a model change re-reads."""
    runner = RecordingRunner(stdout="https://rented.example/v1\n")
    provider = gpu_provider_from_environment(
        {
            "FINEPDFS_GPU_PROVIDER": "command",
            "FINEPDFS_GPU_START_COMMAND": START,
            "FINEPDFS_ENGINE_MODEL": "PaddleOCR-VL-1.6",
        },
        runner=runner,
    )

    with provider.lease() as endpoint:
        assert endpoint.detail == derive_detail(START, "PaddleOCR-VL-1.6")


def test_the_model_a_command_prints_wins_over_the_one_the_run_configured() -> None:
    runner = RecordingRunner(stdout='{"base_url": "https://rented.example/v1", "model": "printed"}')
    provider = gpu_provider_from_environment(
        {
            "FINEPDFS_GPU_PROVIDER": "command",
            "FINEPDFS_GPU_START_COMMAND": START,
            "FINEPDFS_ENGINE_MODEL": "configured",
        },
        runner=runner,
    )

    with provider.lease() as endpoint:
        assert endpoint.detail == derive_detail(START, "printed")


def test_a_derived_detail_is_stable_across_runs_and_follows_the_model() -> None:
    assert derive_detail(START, "m") == derive_detail(START, "m")
    assert derive_detail(START, "m") != derive_detail(START, "other")
    assert derive_detail(START, "m") != derive_detail("another-command", "m")


def test_a_start_command_that_prints_no_endpoint_is_the_providers_own_failure() -> None:
    """This test used to pin CommandFailed, which names an engine's own command failing.

    endpoint_from_start_output reads what the *provider's* start command printed, so its failure is the
    provider's: GpuStartFailed, the type the seam defines for "this run cannot start". CommandFailed
    stays the engine's, which is the distinction GpuStartFailed's docstring draws and the CLI's exit-2
    rule depends on (ADR-0007).
    """
    with pytest.raises(GpuStartFailed, match="printed no endpoint"):
        endpoint_from_start_output("nothing useful here", START)


def test_a_failing_start_command_is_a_provider_failure_of_its_own() -> None:
    """A provider that cannot start is reported by the CLI; an engine that fails later is not this."""
    runner = RecordingRunner(returncode=1)
    provider = CommandGpuProvider(start_command=START, stop_command=STOP, runner=runner)

    with pytest.raises(GpuStartFailed), provider.lease():
        pass

    assert not issubclass(GpuStartFailed, CommandFailed)


def test_a_start_command_that_fails_after_launching_gives_the_worker_back() -> None:
    """A start that exits nonzero can have launched a worker first, so the stop has to run anyway.

    The stop used to be reachable only past a successful start *and* a parsed endpoint, so this exit
    left the worker billing -- the cost ADR-0007's "released on the failure path" is written to
    prevent. The parse failure below is the same hole: nothing about "no endpoint" says "no worker".
    """
    runner = RecordingRunner(returncode=1)
    provider = CommandGpuProvider(start_command=START, stop_command=STOP, runner=runner)

    with pytest.raises(GpuStartFailed, match="start command failed"), provider.lease():
        pass

    assert [call[0] for call in runner.calls] == [START, STOP], "a failed start is still a billable worker"


def test_a_start_command_that_prints_no_endpoint_gives_the_worker_back() -> None:
    runner = RecordingRunner(stdout="nothing useful here")
    provider = CommandGpuProvider(start_command=START, stop_command=STOP, runner=runner)

    with pytest.raises(GpuStartFailed, match="printed no endpoint"), provider.lease():
        pass

    assert [call[0] for call in runner.calls] == [START, STOP]


def test_a_stop_that_fails_after_a_failed_start_does_not_replace_the_start_error(caplog) -> None:
    """The start is what the operator has to act on; the leak is reported beside it, not in its place."""
    runner = RecordingRunner(returncode=1, stop_returncode=1)
    provider = CommandGpuProvider(start_command=START, stop_command=STOP, runner=runner)

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(GpuStartFailed, match="start command failed"),
        provider.lease(),
    ):
        pass

    assert [call[0] for call in runner.calls] == [START, STOP]
    assert "billing" in caplog.text


def test_a_start_command_that_is_not_installed_is_the_providers_own_failure() -> None:
    """The same shape as a nonzero exit: the runner raises CommandFailed before require_success runs.

    A binary that is not installed must not reach the caller as the engine's ``CommandFailed``, because
    "this run cannot start" is the provider's own ``GpuStartFailed`` -- the type the CLI reports and
    exits 2 on, whatever made the start command fail (ADR-0007).
    """
    provider = CommandGpuProvider(start_command="/no/such/start-worker", stop_command=STOP)

    with pytest.raises(GpuStartFailed, match="not installed"), provider.lease():
        pass


def test_a_known_provider_missing_its_start_command_is_misconfigured_not_unknown() -> None:
    """'No such provider' and 'this provider is incomplete' send a reader to different places."""
    with pytest.raises(MisconfiguredGpuProvider, match="FINEPDFS_GPU_START_COMMAND"):
        gpu_provider_from_environment({"FINEPDFS_GPU_PROVIDER": "command"})

    assert not issubclass(MisconfiguredGpuProvider, UnknownGpuProvider)


def test_a_new_provider_is_registered_in_one_place(monkeypatch) -> None:
    """A Modal or RunPod adapter should be one entry the factory and the CLI both read."""
    class ModalProvider:
        @contextmanager
        def lease(self):
            yield GpuEndpoint(base_url="https://modal.example/v1", detail="modal:ocr")

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


def test_exporting_an_endpoint_moves_the_address_but_not_a_pinned_detail() -> None:
    environ = {"FINEPDFS_ENGINE_DETAIL": "pinned:v1"}

    export_endpoint(
        GpuEndpoint(base_url="https://new.example/v1", model="m", api_key="k", detail="provider-derived"),
        environ,
    )

    assert environ["FINEPDFS_ENGINE_BASE_URL"] == "https://new.example/v1"
    assert environ["FINEPDFS_ENGINE_MODEL"] == "m"
    assert environ["FINEPDFS_ENGINE_API_KEY"] == "k"
    assert environ["FINEPDFS_ENGINE_DETAIL"] == "pinned:v1"


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

    assert str(first.identity) == str(second.identity) == "paddleocr-vl@modal:ocr:1.6@dpi200"


def test_without_a_pinned_detail_the_address_is_the_key() -> None:
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


def test_the_runtime_builds_the_engine_from_the_exported_detail() -> None:
    """A lease reaches the flow only through the environment, so that path has to be wired."""
    runtime = runtime_from_environment(
        {
            "FINEPDFS_ENGINE": "paddleocr-vl",
            "FINEPDFS_ENGINE_BASE_URL": "https://rented.example/v1",
            "FINEPDFS_ENGINE_MODEL": "m",
            "FINEPDFS_ENGINE_DETAIL": "modal:ocr:1.6",
        }
    )

    assert str(runtime.engines["paddleocr-vl"].identity) == "paddleocr-vl@modal:ocr:1.6@dpi200"


def test_the_seam_variables_have_one_owner_and_every_reader_reads_it() -> None:
    """A constant is only a single source of truth if the readers read it.

    Renaming a seam variable used to mean three edits -- the constant, the literal runtime.py read and
    the literal cli.py's flag map held -- and a missed one silently ignored the variable. The map
    assertions below pin the CLI to the constants, and the runtime assertion passes only against the
    name the constant holds, so a rename that forgets either reader fails here.
    """
    assert ENVIRONMENT_FOR_ENGINE["engine_detail"] == ENGINE_DETAIL
    assert ENVIRONMENT_FOR_ENGINE["gpu_provider"] == GPU_PROVIDER
    assert ENVIRONMENT_FOR_ENGINE["gpu_start_command"] == GPU_START_COMMAND
    assert ENVIRONMENT_FOR_ENGINE["gpu_stop_command"] == GPU_STOP_COMMAND

    runtime = runtime_from_environment(
        {
            ENGINE_DETAIL: "modal:ocr:1.6",
            ENGINE_BASE_URL: "https://rented.example/v1",
            "FINEPDFS_ENGINE": "paddleocr-vl",
        }
    )

    assert str(runtime.engines["paddleocr-vl"].identity) == "paddleocr-vl@modal:ocr:1.6@dpi200"


def test_a_misconfigured_provider_fails_the_cli_cleanly(monkeypatch, capsys) -> None:
    """A run that cannot start is an operator problem: a message and exit 2, not a traceback."""
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.delenv("FINEPDFS_GPU_START_COMMAND", raising=False)

    code = main(["dry-run", "--engine", "paddleocr-vl"])

    assert code == 2
    assert "FINEPDFS_GPU_START_COMMAND" in capsys.readouterr().err


def test_a_start_command_that_cannot_run_fails_the_cli_cleanly(monkeypatch, capsys) -> None:
    """A start command that exits nonzero cannot start the run, so the CLI reports it and exits 2."""
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.setenv("FINEPDFS_GPU_START_COMMAND", "/bin/false")

    code = main(["dry-run", "--engine", "paddleocr-vl"])

    assert code == 2
    assert "GPU start command" in capsys.readouterr().err


def test_a_malformed_start_command_fails_the_cli_cleanly(monkeypatch, capsys) -> None:
    """shlex.split raises ValueError on unbalanced quotes, which no CLI except clause names.

    A command an operator typed wrong is a provider that cannot run, so it must reach the same exit-2
    message as a start command that exits nonzero -- not a ValueError traceback (ADR-0007).
    """
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.setenv("FINEPDFS_GPU_START_COMMAND", "start-worker 'unclosed")

    code = main(["dry-run", "--engine", "paddleocr-vl"])

    assert code == 2
    assert "malformed" in capsys.readouterr().err


def test_a_non_numeric_gpu_timeout_fails_the_cli_cleanly(monkeypatch, tmp_path, capsys) -> None:
    """``float("soon")`` raises a bare ValueError, which no CLI except clause names.

    A timeout an operator typed wrong is a provider that cannot run, so it must reach the same exit-2
    message as a malformed command -- not a ValueError traceback (ADR-0007). ``MisconfiguredGpuProvider``
    already claims "what it was given is unusable" as its own failure.
    """
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.setenv("FINEPDFS_GPU_START_COMMAND", "/bin/echo https://rented.example/v1")
    monkeypatch.setenv("FINEPDFS_GPU_TIMEOUT", "soon")

    code = main(["dry-run", "--corpus", str(tmp_path / "nothing"), "--engine", "paddleocr-vl"])

    assert code == 2
    assert "FINEPDFS_GPU_TIMEOUT" in capsys.readouterr().err


def test_a_malformed_stop_command_fails_the_cli_cleanly(monkeypatch, tmp_path, capsys) -> None:
    """The stop command is split on the way out too, so a typo there fails the same clean way."""
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.setenv("FINEPDFS_GPU_START_COMMAND", "/bin/echo https://rented.example/v1")
    monkeypatch.setenv("FINEPDFS_GPU_STOP_COMMAND", "stop-worker 'unclosed")

    code = main(["dry-run", "--corpus", str(tmp_path / "nothing"), "--engine", "paddleocr-vl"])

    assert code == 2
    assert "malformed" in capsys.readouterr().err


def test_a_stop_command_that_fails_fails_the_cli_cleanly(monkeypatch, tmp_path, capsys) -> None:
    """A worker the stop command could not give back is a provider failure, the same as a start one."""
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.setenv("FINEPDFS_GPU_START_COMMAND", "/bin/echo https://rented.example/v1")
    monkeypatch.setenv("FINEPDFS_GPU_STOP_COMMAND", "/bin/false")

    code = main(["dry-run", "--corpus", str(tmp_path / "nothing"), "--engine", "paddleocr-vl"])

    assert code == 2
    assert "GPU stop command failed" in capsys.readouterr().err


def test_a_stop_command_that_is_not_installed_fails_the_cli_cleanly(monkeypatch, tmp_path, capsys) -> None:
    """A missing stop binary fails before require_success: SubprocessRunner raises CommandFailed itself.

    That is the runner's failure, not an engine's, so the stop path has to name it the provider's way
    rather than let it out as a bare CommandFailed and a traceback.
    """
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.setenv("FINEPDFS_GPU_START_COMMAND", "/bin/echo https://rented.example/v1")
    monkeypatch.setenv("FINEPDFS_GPU_STOP_COMMAND", "/no/such/stop-worker")

    code = main(["dry-run", "--corpus", str(tmp_path / "nothing"), "--engine", "paddleocr-vl"])

    assert code == 2
    assert "GPU stop command failed" in capsys.readouterr().err
