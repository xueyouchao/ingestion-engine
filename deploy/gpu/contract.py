"""The contract every rented GPU worker speaks, independent of the vendor that rents it.

ADR-0009 keeps the worker outside the package: ``CommandGpuProvider`` runs a start command, the
command prints exactly one JSON object, the run uses the endpoint, and a stop command gives it back.
This module owns the parts that must not be re-decided per vendor:

* the endpoint payload, whose ``base_url`` must carry the ``/v1`` base path because ``ChatEndpoint``
  appends only ``/chat/completions``;
* the deployment detail, which names the model and its versions and never the ephemeral address;
* the lease state, one file per lease and named after the identity the provider gives that lease,
  written as soon as a worker exists and before any readiness wait, so a stop can still find and
  terminate a worker that never finished starting, and cannot find another run's;
* the lease reservation, a sibling lock a start holds from before it reads that lease until the lease
  records its worker, so two hand-run starts that share the unnamed ``manual`` lease cannot both rent a
  worker and lose one of the two ids to the later write;
* redaction of anything secret -- a credential or the leased address -- from text that is about to be
  printed or logged.

It imports no vendor SDK, so the offline suite can test it with no Modal installed (ADR-0009).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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

# --- The deployed app: one container, scale to zero, one bearer (ADR-0012) -------------------------
#
# The demo does not rent a Sandbox per run. It deploys the same pinned image as a Modal Server, capped at
# one container, scaling to zero between visitors, and authenticated with a bearer the app itself checks
# because the pipeline's adapter cannot send Modal's own proxy headers (ADR-0012). The pins live here
# beside the batch worker's, so the two postures cannot drift into two different engines. Nothing in this
# block names an address or a token: the URL belongs to Modal and the bearer belongs in the Secret.
DEPLOYED_APP_NAME = "finepdfs-demo-ocr"
DEPLOYED_SERVER_NAME = "OcrWorkerServer"
DEPLOYED_SECRET_NAME = "finepdfs-demo-ocr-bearer"
# The Secret carries one key, and the launcher reads that key by this name; the test pins the two modules
# to the same string, because a Secret injected under another name is a server that answers without a bearer.
DEPLOYED_SECRET_VARIABLE = "PADDLEOCR_VL_API_KEY"
DEPLOYED_GPU = "L4"
# One GPU as the ceiling and nothing at rest. The floor is what the Warm Control moves, without a redeploy.
DEPLOYED_MAX_CONTAINERS = 1
DEPLOYED_MIN_CONTAINERS = 0
WARM_FLOOR_CONTAINERS = 1
# Low minutes, generous enough that a visitor who reloads hits the same container, short enough that an
# idle demo holds no GPU for long.
DEPLOYED_SCALEDOWN_WINDOW = 180
# The first whole Sandbox run took 450 s including the first weights download (ADR-0009), so a cold start
# of a deployed container that must load weights can plausibly take several minutes. This bounds it.
DEPLOYED_STARTUP_TIMEOUT = 1200
# Modal gives the exit hook this long to stop vLLM before the container goes away.
DEPLOYED_EXIT_GRACE_PERIOD = 60

# Modal's published L4 price, observed on 2026-10-19. The source and the date are pins too: the Warm
# Control's number and the runbook's number have to come from the same observation, not from memory.
GPU_RATE_PER_SECOND_USD = 0.000222
GPU_RATE_SOURCE = "https://modal.com/pricing"
GPU_RATE_OBSERVED = "2026-10-19"


@dataclass(frozen=True, slots=True)
class WarmFloorCost:
    """What a held-warm floor costs: computed here so the command and the runbook cannot disagree.

    ``containers`` is the floor, not the ceiling: zero costs nothing, and one is Modal's published
    per-second L4 rate for every second the container exists. The two formatters are the only spellings
    of the number an operator should see.
    """

    containers: int
    per_minute_usd: float
    per_hour_usd: float

    def per_minute(self) -> str:
        return f"${self.per_minute_usd:,.4f}"

    def per_hour(self) -> str:
        return f"${self.per_hour_usd:,.2f}"


def warm_floor_cost(containers: int = WARM_FLOOR_CONTAINERS) -> WarmFloorCost:
    """The per-minute and per-hour cost of keeping ``containers`` warm, at the pinned L4 rate."""
    if containers < 0:
        raise WorkerContractError(f"a warm floor cannot be negative, got {containers}")
    per_second = GPU_RATE_PER_SECOND_USD * containers
    return WarmFloorCost(
        containers=containers,
        per_minute_usd=per_second * 60,
        per_hour_usd=per_second * 3600,
    )

# One lease, one state file. The provider mints an identity per lease and passes it to the start and the
# stop command alike (``FINEPDFS_GPU_LEASE_ID``); the state file is named after it, so two runs record two
# workers and neither can find, overwrite or terminate the other's (ADR-0007).
LEASE_ID_VARIABLE = "FINEPDFS_GPU_LEASE_ID"
STATE_DIR_VARIABLE = "FINEPDFS_GPU_STATE_DIR"
DEFAULT_STATE_DIR = "~/.cache/finepdfs/gpu-workers"
# A start run by hand has no provider to mint an identity. It records under this fixed name, which is
# what keeps `start` then `stop` from one shell working.
UNIDENTIFIED_LEASE = "manual"
LEASE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

# A start owns its lease between reading it and recording its worker, and a hand-run start's lease is the
# fixed `manual` name: without ownership two starts read no lease, both rent a worker, and the later write
# drops the earlier id, leaving a GPU billing with no handle stop can find. The reservation is an OS lock
# (`flock`) on a sibling file with a non-JSON suffix. The OS releases it when the holding descriptor closes
# or the process dies, so recovery from a crash is the kernel's job, and a file left behind names no owner
# anybody has to trust: the pid that wrote it may belong to an unrelated process by the time it is read.
RESERVATION_SUFFIX = ".lock"

# Environment names whose values must never reach stdout, stderr or a log line.
SECRET_MARKERS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")
# The leased address is a credential too: the tunnel is unguessable and is the only thing protecting the
# worker, so it never goes into an error or a log line (the runbook says so, and this enforces it). Any
# scheme is masked, not only http(s): a value rejected for a mistyped scheme would otherwise print the
# host, and so would a vendor error that quotes one.
URL_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://[^\s\"'<>]+")


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


def _mask_urls(text: str) -> str:
    """Replace an http(s) address with its scheme, so a message names the fault and not the address."""
    return URL_PATTERN.sub(lambda match: f"{match.group(1)}://***", text)


def _require_base_url(base_url: str) -> None:
    if not base_url or not base_url.strip():
        raise WorkerContractError("the worker endpoint has no base_url")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WorkerContractError(f"the worker base_url is not an http(s) address: {_mask_urls(base_url)!r}")
    if parsed.path.rstrip("/") != "/v1":
        raise WorkerContractError(
            f"the worker base_url must end at the /v1 base path, got {_mask_urls(base_url)!r}"
        )
    if parsed.query or parsed.fragment:
        raise WorkerContractError(
            f"the worker base_url must not carry a query or fragment: {_mask_urls(base_url)!r}"
        )


def _require_model(model: str) -> None:
    if not model or not model.strip() or any(character.isspace() for character in model):
        raise WorkerContractError(f"the worker model must be one non-empty name, got {model!r}")


def _require_detail(detail: str) -> None:
    """A detail is one token of the shape ``model@version[:attribute...]`` and never an address.

    The shape is what makes the detail usable as the deployment half of the Engine Identity: a bare
    name says nothing about which weights served the text, so a rebuilt worker would keep the memo key
    and the corpus would never be re-read (ADR-0005, ADR-0009).
    """
    if not detail or not detail.strip():
        raise WorkerContractError("the worker detail is empty; it must name the model and its versions")
    if "://" in detail or detail.lower().startswith(("http:", "https:")):
        raise WorkerContractError(f"the worker detail must not contain a URL: {_mask_urls(detail)!r}")
    if any(character.isspace() for character in detail):
        raise WorkerContractError(f"the worker detail must be one token, got {_mask_urls(detail)!r}")
    model, _, attributes = detail.partition("@")
    version = attributes.partition(":")[0]
    if detail.count("@") != 1 or not model or not version:
        raise WorkerContractError(
            "the worker detail must name the model and its version as model@version, "
            f"got {_mask_urls(detail)!r}"
        )


@dataclass(frozen=True, slots=True)
class LeaseState:
    """What must survive the start command: enough to terminate the worker from another process.

    Two fields, and both are load-bearing: an id is what the vendor's terminate-by-id needs, and the
    provider is what makes a stop refuse another vendor's id instead of guessing what it means. Neither
    is a secret and neither is an address.
    """

    provider: str
    worker_id: str

    def __post_init__(self) -> None:
        if not self.provider:
            raise WorkerContractError("the lease state has no provider")
        if not self.worker_id:
            raise WorkerContractError("the lease state has no worker id")

    def to_payload(self) -> dict[str, str]:
        return {"provider": self.provider, "worker_id": self.worker_id}

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
        return cls(provider=provider, worker_id=worker_id)


def state_directory(environ: Mapping[str, str]) -> Path:
    """The directory that holds one state file per lease.

    An empty value is not a path: it is the unset value every other optional variable here reads as
    absent, so it falls back to the default rather than becoming ``.`` -- a directory that stop would
    then report as an unreadable lease.
    """
    configured = environ.get(STATE_DIR_VARIABLE, "").strip()
    return Path(configured or DEFAULT_STATE_DIR).expanduser()


def lease_file(environ: Mapping[str, str]) -> Path:
    """The state file that records *this* lease, named by the identity its provider passed.

    Per lease, not per machine. A single shared file is what let one run's stop terminate another run's
    live worker: both commands read the same path, so the second stop found the first run's worker id.
    An identity is validated as one file-name component, so a value from the environment cannot name a
    path outside the state directory. An empty identity is a start run by hand, which records under the
    fixed ``manual`` name so one start and the stop that follows it in the same shell still meet.
    """
    lease_id = environ.get(LEASE_ID_VARIABLE, "").strip()
    if not lease_id:
        return state_directory(environ) / f"{UNIDENTIFIED_LEASE}.json"
    if not LEASE_ID_PATTERN.fullmatch(lease_id):
        raise WorkerContractError(
            f"{LEASE_ID_VARIABLE} is not a usable lease identity "
            f"(letters, digits, '-' and '_', at most 64): {lease_id!r}"
        )
    return state_directory(environ) / f"{lease_id}.json"


def reservation_path(lease_path: Path) -> Path:
    """The sibling file that reserves ``lease_path`` while a start rents its worker.

    A sibling because it guards one lease, and non-JSON because it is not a lease: every reader here
    looks for ``*.json``, so a lock file that outlives the start that created it is never read as a
    worker id.
    """
    return lease_path.with_name(lease_path.name + RESERVATION_SUFFIX)


@contextmanager
def lease_reservation(lease_path: Path) -> Iterator[Path]:
    """Hold ``lease_path`` from before its lease is read until the lease records the worker.

    That is the whole gate -- read, check, create, write -- and holding it is what protects the one case
    the provider's per-lease identity cannot: two starts run by hand, which both name the same file. The
    hold is an OS lock (``flock``) on the sibling file, so the kernel is the only judge of who holds it:

    * a second start is refused, in another process or in another thread of this one, because each
      acquisition opens its own descriptor and ``flock`` locks are per descriptor, not per process;
    * a start that crashes releases it, because the kernel drops a lock when the holding descriptor goes
      away. No pid is read and no liveness is guessed at: the pid a dead start left in the file can belong
      to an unrelated process by the time anyone looks, so trusting it is how a lease gets blocked for ever;
    * the file is closed and never unlinked. Unlinking would let a second start create a *new* file and
      lock a different inode while the first still holds the old one -- the split lock, reintroduced.

    A leftover ``.lock`` is therefore harmless bookkeeping: it is not JSON, and every reader here looks for
    ``*.json``, so a lock file can never be mistaken for a lease.
    """
    reservation = reservation_path(lease_path)
    try:
        reservation.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _uncreatable(reservation, error) from error
    try:
        descriptor = os.open(reservation, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        raise _uncreatable(reservation, error) from error
    # The descriptor is not inheritable (``os.open`` sets close-on-exec), so nothing this start launches
    # can outlive it holding the lock: the reservation ends with the process that took it.
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # The holder is unnameable on purpose: this file says nothing about who holds the lock now.
            raise WorkerContractError(
                f"another start is reserving the GPU lease at {lease_path}; "
                "wait for it to finish or check whether it is still running"
            ) from None
        yield reservation
    finally:
        os.close(descriptor)


def _uncreatable(reservation: Path, error: OSError) -> WorkerContractError:
    """One message for the directory that cannot hold a reservation and the file that cannot be written."""
    return WorkerContractError(
        f"the GPU lease reservation at {reservation} cannot be created: {error}"
    )


def positive_seconds(environ: Mapping[str, str], name: str, default: int) -> int:
    """A positive whole number of seconds, where an empty or blank value is the unset value.

    Empty is absent everywhere else on this boundary -- the state directory, a pinned detail -- so a
    variable exported with no value must mean the default rather than fail the start that read it:
    ``int("")`` used to report the empty string as "not a whole number of seconds".
    """
    raw = environ.get(name, "").strip() or str(default)
    try:
        seconds = int(raw)
    except ValueError as error:
        raise WorkerContractError(f"{name} is not a whole number of seconds: {raw!r}") from error
    if seconds <= 0:
        raise WorkerContractError(f"{name} must be positive, got {seconds}")
    return seconds


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
    """Replace credentials before text reaches a terminal or a log.

    Two things are credentials here. A value under a credential-looking name, which a vendor SDK error
    can quote; and any http(s) address, because the leased tunnel URL *is* the credential the runbook
    describes. The address is masked by shape, not by name: nothing in the environment has to be
    declared secret for it to be hidden, and an address a vendor error invented is hidden too.
    """
    redacted = text
    for name, value in environ.items():
        if len(value) < 4 or not any(marker in name.upper() for marker in SECRET_MARKERS):
            continue
        redacted = redacted.replace(value, "***")
    return _mask_urls(redacted)
