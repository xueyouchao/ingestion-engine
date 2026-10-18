"""The contract every rented GPU worker speaks, independent of the vendor that rents it.

ADR-0009 keeps the worker outside the package: ``CommandGpuProvider`` runs a start command, the
command prints exactly one JSON object, the run uses the endpoint, and a stop command gives it back.
This module owns the parts that must not be re-decided per vendor:

* the endpoint payload, whose ``base_url`` must carry the ``/v1`` base path because ``ChatEndpoint``
  appends only ``/chat/completions``;
* the deployment detail, which names the model and its versions and never the ephemeral address;
* the lease state, written as soon as a worker exists and before any readiness wait, so a stop can
  still find and terminate a worker that never finished starting;
* redaction of anything secret from text that is about to be printed or logged.

It imports no vendor SDK, so the offline suite can test it with no Modal installed (ADR-0009).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

# --- The model the worker serves ---------------------------------------------------------------
#
# Pinned, not floating (CODING_STANDARDS, "a published record must be reproducible"). The vLLM recipe
# for this model asks for vLLM >= 0.11.1, and the served name is the one clients must ask for. The
# image is pinned by tag *and* the linux/amd64 manifest digest observed on 2026-10-18. The weights are
# pinned by the Hugging Face commit the model repository resolved to on the same date, so a rebuilt
# worker serves the same bytes and reports the same detail.
WORKER_MODEL = "PaddleOCR-VL-1.6"
MODEL_ID = "PaddlePaddle/PaddleOCR-VL-1.6"
MODEL_REVISION = "c5630abae1d940eafe0697512a0325494b02ab42"
VLLM_VERSION = "0.32.0"
VLLM_IMAGE_DIGEST = "6ee133356d49ed24cee03dd0cda3d3980279a11c107ce3e752c89e76540f41c9"
VLLM_IMAGE = f"vllm/vllm-openai:v{VLLM_VERSION}@sha256:{VLLM_IMAGE_DIGEST}"

# The worker listens on this container port; the provider tunnel maps it to HTTPS.
WORKER_PORT = 8000

# The deployment half of the Engine Identity. It names the model, the weights commit and the image
# digest, and deliberately contains no address: a rented worker answers on a new host every run, and
# an address here would re-read the whole corpus (ADR-0005, ADR-0007).
WORKER_DETAIL = (
    f"paddleocr-vl@1.6:weights-{MODEL_REVISION[:12]}:image-{VLLM_IMAGE_DIGEST[:12]}"
)

PROVIDER_MODAL = "modal"

STATE_FILE = "FINEPDFS_GPU_STATE_FILE"
DEFAULT_STATE_FILE = "~/.cache/finepdfs/gpu-worker.json"

# Environment names whose values must never reach stdout, stderr or a log line.
SECRET_MARKERS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")


class WorkerContractError(RuntimeError):
    """Raised when a worker endpoint, a lease file or a settings value does not satisfy the contract.

    One class, not one per field: at this boundary every failure is the same operator-facing outcome
    -- the worker cannot be described or found -- and the message names the field that is wrong.
    """


def base_url_with_v1(address: str) -> str:
    """The OpenAI-compatible base URL for a worker tunnel, always ending at ``/v1``.

    ``ChatEndpoint`` appends only ``/chat/completions``, so a bare host would send OCR requests to
    ``/chat/completions`` instead of ``/v1/chat/completions`` (ADR-0009). A tunnel URL names a host and
    no path, so ``/v1`` is appended; an address that already ends there is left alone.
    """
    stripped = address.strip().rstrip("/")
    if not stripped:
        raise WorkerContractError("the worker tunnel has no address")
    if stripped.endswith("/v1"):
        return stripped
    return stripped + "/v1"


def endpoint_payload(
    base_url: str,
    model: str = WORKER_MODEL,
    detail: str = WORKER_DETAIL,
) -> dict[str, str]:
    """The one JSON object a start command prints: where the worker is, what it serves, and its name.

    Validation is part of the contract rather than a nicety: the provider trusts this payload, so an
    address without ``/v1`` or a detail that carries the address must fail in the worker that produced
    it, not in a memo key that silently changes every run.
    """
    _require_base_url(base_url)
    _require_model(model)
    _require_detail(detail)
    return {"base_url": base_url, "model": model, "detail": detail}


def endpoint_line(payload: Mapping[str, str]) -> str:
    """One line of JSON, which is all the start command may write to stdout."""
    return json.dumps(dict(payload), separators=(",", ":"))


def emit_endpoint(payload: Mapping[str, str], stream: TextIO | None = None) -> str:
    """Print the endpoint as exactly one JSON object, and return the line that was written."""
    line = endpoint_line(payload)
    print(line, file=stream if stream is not None else sys.stdout)
    return line


def _require_base_url(base_url: str) -> None:
    if not base_url or not base_url.strip():
        raise WorkerContractError("the worker endpoint has no base_url")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WorkerContractError(f"the worker base_url is not an http(s) address: {base_url!r}")
    if parsed.path.rstrip("/") != "/v1":
        raise WorkerContractError(
            f"the worker base_url must end at the /v1 base path, got {base_url!r}"
        )
    if parsed.query or parsed.fragment:
        raise WorkerContractError(f"the worker base_url must not carry a query or fragment: {base_url!r}")


def _require_model(model: str) -> None:
    if not model or not model.strip() or any(character.isspace() for character in model):
        raise WorkerContractError(f"the worker model must be one non-empty name, got {model!r}")


def _require_detail(detail: str) -> None:
    if not detail or not detail.strip():
        raise WorkerContractError("the worker detail is empty; it must name the model and its versions")
    if "://" in detail or detail.lower().startswith(("http:", "https:")):
        raise WorkerContractError(f"the worker detail must not contain a URL: {detail!r}")
    if any(character.isspace() for character in detail):
        raise WorkerContractError(f"the worker detail must be one token, got {detail!r}")


@dataclass(frozen=True, slots=True)
class LeaseState:
    """What must survive the start command: enough to terminate the worker from another process.

    Only the provider and the worker id are load-bearing. ``app_name`` is recorded because Modal
    sandboxes belong to an app and an operator may need to find the worker by app; ``created_at`` is
    there to age out a stale lease. Neither is a secret and neither is an address.
    """

    provider: str
    worker_id: str
    app_name: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.provider:
            raise WorkerContractError("the lease state has no provider")
        if not self.worker_id:
            raise WorkerContractError("the lease state has no worker id")

    def to_payload(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "worker_id": self.worker_id,
            "app_name": self.app_name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> LeaseState:
        if not isinstance(payload, dict):
            raise WorkerContractError("the lease state is not a JSON object")
        provider = payload.get("provider")
        worker_id = payload.get("worker_id")
        if not isinstance(provider, str) or not provider:
            raise WorkerContractError("the lease state does not name its provider")
        if not isinstance(worker_id, str) or not worker_id:
            raise WorkerContractError("the lease state does not name its worker_id")
        app_name = payload.get("app_name") or ""
        created_at = payload.get("created_at") or ""
        if not isinstance(app_name, str) or not isinstance(created_at, str):
            raise WorkerContractError("the lease state has a non-string app_name or created_at")
        return cls(provider=provider, worker_id=worker_id, app_name=app_name, created_at=created_at)


def state_path(environ: Mapping[str, str]) -> Path:
    """Where this machine records the one worker it currently leases.

    An empty value is not a path: it is the unset value every other optional variable here reads as
    absent, so it falls back to the default rather than becoming ``.`` -- a directory that stop would
    then report as an unreadable lease.
    """
    configured = environ.get(STATE_FILE, "").strip()
    return Path(configured or DEFAULT_STATE_FILE).expanduser()


def write_lease(path: Path, state: LeaseState) -> None:
    """Write the lease atomically, so a crash never leaves a half-written id that stop cannot read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.tmp")
    scratch.write_text(json.dumps(state.to_payload(), indent=2) + "\n", encoding="utf-8")
    os.replace(scratch, path)


def read_lease(path: Path) -> LeaseState | None:
    """The recorded lease, or ``None`` when no worker is recorded at that path.

    A file that exists but cannot be understood is an error, not ``None``: something may still be
    billing, and the operator needs to see the file that should have said what it is.
    """
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WorkerContractError(f"the GPU lease state at {path} cannot be read as JSON: {error}") from error
    try:
        return LeaseState.from_payload(payload)
    except WorkerContractError as error:
        raise WorkerContractError(f"the GPU lease state at {path} is invalid: {error}") from error


def clear_lease(path: Path) -> None:
    """Forget a worker that has been terminated. A missing file is already the desired state."""
    path.unlink(missing_ok=True)


def redact(text: str, environ: Mapping[str, str]) -> str:
    """Replace credential-looking environment values before text reaches a terminal or a log.

    The worker never needs to print a secret, but a vendor SDK error can quote one. This is the last
    line of defence at the script boundary, not a licence to pass secrets around (ADR-0009).
    """
    redacted = text
    for name, value in environ.items():
        if len(value) < 4 or not any(marker in name.upper() for marker in SECRET_MARKERS):
            continue
        redacted = redacted.replace(value, "***")
    return redacted
