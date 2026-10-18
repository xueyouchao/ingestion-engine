"""Launch the one upstream server that serves PaddleOCR-VL behind ``/v1``, inside the worker image.

The routes the pipeline uses are ``POST /v1/chat/completions`` (one page image, prompt ``"OCR:"``) and
``GET /v1/models`` (readiness and the served model name). Those are vLLM's OpenAI-compatible server
routes, so this file does not implement HTTP itself: it launches vLLM with the pinned model, revision
and served name. That keeps one server, one protocol and one place where the weights version enters the
deployment detail.

The exact upstream invocation is the one assumption this offline slice cannot execute (ADR-0009). It
is therefore a single documented command, overridable by one environment variable, and every argument
is visible through ``--print-command``:

    python server.py --print-command

``PADDLEOCR_VL_SERVE_COMMAND`` overrides the whole argv as a JSON array of strings, for the day the
upstream command changes or a compatible managed endpoint is served instead.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO

SERVE_COMMAND = "PADDLEOCR_VL_SERVE_COMMAND"
MODEL_VARIABLE = "PADDLEOCR_VL_MODEL"
REVISION_VARIABLE = "PADDLEOCR_VL_REVISION"
SERVED_MODEL_VARIABLE = "PADDLEOCR_VL_SERVED_MODEL_NAME"
PORT_VARIABLE = "PADDLEOCR_VL_PORT"

DEFAULT_PORT = "8000"

# Arguments from the vLLM recipe for PaddleOCR-VL-1.6. OCR workloads do not benefit from prefix
# caching or image reuse, so both are disabled as the recipe recommends.
RECIPE_ARGUMENTS = (
    "--trust-remote-code",
    "--max-num-batched-tokens",
    "16384",
    "--no-enable-prefix-caching",
    "--mm-processor-cache-gb",
    "0",
)


class ServerConfigurationError(RuntimeError):
    """Raised when the launcher cannot build a complete command from the environment it was given."""


def serve_argv(environ: Mapping[str, str]) -> list[str]:
    """The argv the worker runs: one documented command, or the documented override."""
    override = environ.get(SERVE_COMMAND, "").strip()
    if override:
        return _override_argv(override)
    model = _required(environ, MODEL_VARIABLE)
    revision = _required(environ, REVISION_VARIABLE)
    served_model = _required(environ, SERVED_MODEL_VARIABLE)
    port = environ.get(PORT_VARIABLE, DEFAULT_PORT).strip() or DEFAULT_PORT
    if not port.isdigit():
        raise ServerConfigurationError(f"{PORT_VARIABLE} is not a port number: {port!r}")
    return [
        "vllm",
        "serve",
        model,
        "--revision",
        revision,
        "--served-model-name",
        served_model,
        "--host",
        "0.0.0.0",
        "--port",
        port,
        *RECIPE_ARGUMENTS,
    ]


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    execvp: Callable[[str, list[str]], object] = os.execvp,
    stream: TextIO | None = None,
) -> int:
    """Print the command for an operator, or become it. Never returns on the serving path."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    values = os.environ if environ is None else environ
    if arguments == ["--print-command"]:
        print(json.dumps(serve_argv(values), separators=(",", ":")), file=stream or sys.stdout)
        return 0
    if arguments:
        print(f"server.py takes no arguments except --print-command, got {arguments!r}", file=sys.stderr)
        return 2
    command = serve_argv(values)
    execvp(command[0], command)
    return 0


def _override_argv(override: str) -> list[str]:
    try:
        payload = json.loads(override)
    except ValueError as error:
        raise ServerConfigurationError(
            f"{SERVE_COMMAND} is not a JSON array of strings: {override!r} ({error})"
        ) from error
    if not isinstance(payload, list) or not payload or not all(
        isinstance(token, str) and token for token in payload
    ):
        raise ServerConfigurationError(f"{SERVE_COMMAND} must be a non-empty JSON array of strings: {override!r}")
    return list(payload)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ServerConfigurationError(f"the worker image must set {name}")
    return value


def _cli() -> int:
    """The script boundary: a configuration error is a message and an exit code, not a traceback."""
    try:
        return main()
    except ServerConfigurationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
