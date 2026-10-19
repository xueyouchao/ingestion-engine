"""The in-container launcher: one pinned upstream command, visible before it is executed.

The launcher is the one assumption this offline slice cannot execute, so the tests pin everything that
can be checked offline: the model, revision, served name, port and recipe flags of the one command the
image runs. The routes themselves (``/v1/models``, ``/v1/chat/completions``) are vLLM's, and the start
command checks ``/v1/models`` before it prints an endpoint.
"""

from __future__ import annotations

import io
import json

import pytest

from deploy.gpu.contract import MODEL_ID, MODEL_REVISION, WORKER_MODEL, WORKER_PORT
from deploy.gpu.server import (
    API_KEY_VARIABLE,
    MODEL_VARIABLE,
    PORT_VARIABLE,
    REVISION_VARIABLE,
    SERVED_MODEL_VARIABLE,
    ServerConfigurationError,
    main,
    serve_argv,
)


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        MODEL_VARIABLE: MODEL_ID,
        REVISION_VARIABLE: MODEL_REVISION,
        SERVED_MODEL_VARIABLE: WORKER_MODEL,
        PORT_VARIABLE: str(WORKER_PORT),
    }
    values.update(overrides)
    return values


def test_the_default_command_pins_the_model_revision_and_served_name() -> None:
    argv = serve_argv(_environment())

    assert argv[:2] == ["vllm", "serve"]
    assert argv[2] == "PaddlePaddle/PaddleOCR-VL-1.6"
    assert argv[argv.index("--revision") + 1] == MODEL_REVISION
    assert argv[argv.index("--served-model-name") + 1] == WORKER_MODEL
    assert argv[argv.index("--port") + 1] == str(WORKER_PORT)
    assert "--trust-remote-code" in argv
    assert argv[argv.index("--max-num-batched-tokens") + 1] == "16384"


def test_the_command_can_be_inspected_without_launching_anything() -> None:
    stream = io.StringIO()
    launched: list[str] = []

    def never_launch(program: str, argv: list[str]) -> None:
        launched.append(program)

    assert main(["--print-command"], _environment(), never_launch, stream) == 0

    assert launched == [], "printing the command must not start the model"
    assert json.loads(stream.getvalue()) == serve_argv(_environment())


@pytest.mark.parametrize("variable", [MODEL_VARIABLE, REVISION_VARIABLE, SERVED_MODEL_VARIABLE])
def test_a_missing_pin_is_an_error_that_names_it(variable: str) -> None:
    environ = _environment()
    del environ[variable]

    with pytest.raises(ServerConfigurationError, match=variable):
        serve_argv(environ)


def test_a_non_numeric_port_is_refused() -> None:
    with pytest.raises(ServerConfigurationError, match=PORT_VARIABLE):
        serve_argv(_environment(**{PORT_VARIABLE: "http"}))


def test_serving_becomes_the_command_with_the_configured_environment() -> None:
    launched: list[tuple[str, list[str]]] = []

    def launch(program: str, argv: list[str]) -> None:
        launched.append((program, argv))

    assert main([], _environment(), launch) == 0

    assert launched == [("vllm", serve_argv(_environment()))]


def test_an_unexpected_argument_is_a_usage_error() -> None:
    assert main(["--serve-now"], _environment(), lambda *_: None) == 2


def test_the_launcher_passes_the_bearer_to_vllm_when_one_is_set() -> None:
    argv = serve_argv(_environment(**{API_KEY_VARIABLE: "s3cret-bearer"}))

    assert argv[argv.index("--api-key") + 1] == "s3cret-bearer"


def test_the_launcher_sends_no_bearer_when_none_is_set() -> None:
    assert "--api-key" not in serve_argv(_environment())


def test_the_printed_command_masks_the_bearer() -> None:
    """An operator pasting ``--print-command`` must not paste the token into a shell history."""
    stream = io.StringIO()
    launched: list[str] = []

    def never_launch(program: str, argv: list[str]) -> None:
        launched.append(program)

    code = main(["--print-command"], _environment(**{API_KEY_VARIABLE: "s3cret-bearer"}), never_launch, stream)

    assert code == 0
    assert launched == []
    printed = stream.getvalue()
    assert "s3cret-bearer" not in printed
    argv = json.loads(printed)
    assert argv[argv.index("--api-key") + 1] == "***"
    assert argv[argv.index("--served-model-name") + 1] == WORKER_MODEL
