"""The live upload demo's HTTP server, and the composition root that puts its GPU in reach.

A visitor uploads a PDF, the demo routes it with the pipeline's own policy, and the page shows the
decision and the text the chosen lane read. Uploads are jobs, not long requests (ADR-0012): the POST
answers ``202`` with an id, the page polls that id for its stage, and the Reading Queue beside this
module runs one Reading at a time so the per-Reading environment lease is safe.

This module is the composition root. It reads the demo's own limits from ``FINEPDFS_DEMO_*``, leases the
GPU this demo was configured for, and builds the engine, the routing thresholds and the endpoint probe
from the same ``FINEPDFS_*`` names a run reads -- through ``infrastructure.engines.environment``, which is
the one reader both consumers call, so the demo and a run cannot drift into different engines. Pointing
the demo at a leased worker instead of a deployed one is the provider variable alone; no demo-specific
engine variable is introduced.

Nothing here imports a vendor SDK, and nothing here writes: the uploaded bytes are opened from memory,
the page images are rendered from memory, and no file, page or job outlives the process.

    finepdfs-live-demo --host 127.0.0.1 --port 8766
    PYTHONPATH=src python -m finepdfs.interfaces.demo_server
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from finepdfs.application.curate import CurationPolicy
from finepdfs.application.demo_read import DemoRead, ReadingOutcome, Refusal
from finepdfs.domain.demo import (
    DEFAULT_DAILY_OCR_PAGE_BUDGET,
    DEFAULT_PAGE_CAP,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_QUEUE_DEPTH,
    DEFAULT_RATE_LIMIT_READS,
    DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    DEFAULT_UPLOAD_BYTES,
    DEFAULT_WARM_TIMEOUT_SECONDS,
    DemoLimits,
    ReadingStage,
    SpendGatePolicy,
)
from finepdfs.domain.ports import GateLedger
from finepdfs.infrastructure.compute.endpoint import ENGINE_API_KEY, ENGINE_BASE_URL
from finepdfs.infrastructure.compute.ocr_endpoint import HttpOcrEndpointProbe
from finepdfs.infrastructure.compute.providers import (
    GPU_BASE_URL,
    GPU_START_COMMAND,
    leased_environment,
)
from finepdfs.infrastructure.demo.ledger import InMemoryGateLedger
from finepdfs.infrastructure.engines.environment import engine_config, ocr_routing_policy
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextLayerProbe
from finepdfs.infrastructure.engines.registry import NATIVE_ENGINE, EngineConfig, build_engines
from finepdfs.interfaces.demo_jobs import QueueFull, Reader, ReadingJob, ReadingQueue
from finepdfs.interfaces.demo_page import DEMO_HTML

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
# The explorer owns 8765; the live demo is the other page an operator may have open at once.
DEFAULT_PORT = 8766

# A run defaults to the free text layer; the demo's OCR lane defaults to the deployed engine (ADR-0012).
DEFAULT_ENGINE = "paddleocr-vl"
ENGINE = "FINEPDFS_ENGINE"

# The demo's own names. One module answers "what does the demo allow?", so the defaults beside them in
# ``domain.demo`` and the runbook's table cannot disagree with the server's reading.
PAGE_CAP = "FINEPDFS_DEMO_PAGE_CAP"
RATE_LIMIT_READS = "FINEPDFS_DEMO_RATE_LIMIT_READS"
RATE_LIMIT_WINDOW_SECONDS = "FINEPDFS_DEMO_RATE_LIMIT_WINDOW_SECONDS"
DAILY_OCR_PAGES = "FINEPDFS_DEMO_DAILY_OCR_PAGES"
UPLOAD_BYTES = "FINEPDFS_DEMO_UPLOAD_BYTES"
QUEUE_DEPTH = "FINEPDFS_DEMO_QUEUE_DEPTH"
WARM_TIMEOUT_SECONDS = "FINEPDFS_DEMO_WARM_TIMEOUT_SECONDS"
POLL_INTERVAL_SECONDS = "FINEPDFS_DEMO_POLL_INTERVAL_SECONDS"

# What the upload answers before it is a Reading: the media type a browser sends for a PDF, and the name
# the engine's record carries while the document is in memory.
PDF_MEDIA_TYPE = "application/pdf"
UPLOAD_NAME_HEADER = "X-Upload-Name"
DEFAULT_UPLOAD_NAME = "upload.pdf"

# The refusals the handler raises before the use case ever sees the bytes. ``upload`` is "this is not an
# upload the demo accepts" -- the wrong media type, no declared length, over the byte cap, empty, or short
# -- and ``unreadable`` stays the use case's own word for bytes a PDF reader could not open.
UPLOAD = "upload"
NO_CLIENT = ""


class DemoLimitError(ValueError):
    """A demo limit in the environment is not the kind of number that limit takes."""


@dataclass(frozen=True, slots=True)
class _Limit:
    """One demo limit: its variable, its default, the kind of number it is, and what it is counted in.

    ``noun`` earns its place twice: it is the help text of the flag that sets this limit, and it is the
    phrase an operator reads in the runbook, so the two cannot describe the same number differently.
    """

    variable: str
    default: float
    whole: bool
    noun: str


_LIMITS = {
    "page_cap": _Limit(PAGE_CAP, DEFAULT_PAGE_CAP, whole=True, noun="pages per upload"),
    "rate_limit_reads": _Limit(
        RATE_LIMIT_READS, DEFAULT_RATE_LIMIT_READS, whole=True, noun="reads per client per window"
    ),
    "rate_limit_window_seconds": _Limit(
        RATE_LIMIT_WINDOW_SECONDS,
        DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
        whole=False,
        noun="the rate-limit window in seconds",
    ),
    "daily_ocr_page_budget": _Limit(
        DAILY_OCR_PAGES, DEFAULT_DAILY_OCR_PAGE_BUDGET, whole=True, noun="OCR pages per UTC day"
    ),
    "upload_bytes": _Limit(UPLOAD_BYTES, DEFAULT_UPLOAD_BYTES, whole=True, noun="largest upload in bytes"),
    "queue_depth": _Limit(
        QUEUE_DEPTH, DEFAULT_QUEUE_DEPTH, whole=True, noun="Readings waiting behind the one running"
    ),
    "warm_timeout_seconds": _Limit(
        WARM_TIMEOUT_SECONDS, DEFAULT_WARM_TIMEOUT_SECONDS, whole=False, noun="seconds to wait for a cold worker"
    ),
    "poll_interval_seconds": _Limit(
        POLL_INTERVAL_SECONDS, DEFAULT_POLL_INTERVAL_SECONDS, whole=False, noun="seconds between readiness polls"
    ),
}

# The console script's overrides, named for the limit they set rather than for the variable, so an
# operator changes the spend posture without knowing the environment layout.
_FLAGS = {
    "page_cap": "--page-cap",
    "rate_limit_reads": "--rate-limit-reads",
    "rate_limit_window_seconds": "--rate-limit-window-seconds",
    "daily_ocr_page_budget": "--daily-ocr-pages",
    "upload_bytes": "--upload-bytes",
    "queue_depth": "--queue-depth",
    "warm_timeout_seconds": "--warm-timeout",
    "poll_interval_seconds": "--poll-interval",
}


def demo_limits(values: Mapping[str, str]) -> DemoLimits:
    """The demo's limits: each the spec's default, overridden by the ``FINEPDFS_DEMO_*`` it names.

    A variable exported with no value means the default, exactly as it does for the engine's reader: an
    operator clearing a variable asks for the demo's own posture, not for a refusal to parse.
    """
    configured: dict[str, Any] = {}
    for field, limit in _LIMITS.items():
        raw = values.get(limit.variable, "").strip()
        configured[field] = _number(limit, raw) if raw else limit.default
    return DemoLimits(**configured)


def configured_engine(values: Mapping[str, str]) -> str:
    """The engine the demo's OCR lane asks for: ``FINEPDFS_ENGINE``, else the deployed one.

    A run defaults to the native text layer; the demo is the opposite case, because its whole point is
    the OCR lane's decision. The name is still read from the one variable a run reads, so pointing the
    demo at another engine is not a second knob.
    """
    return values.get(ENGINE, "").strip() or DEFAULT_ENGINE


class DemoApp:
    """One demo server's worth of state: its limits, its ledger, and its single-slot Reading Queue.

    ``ledger``, ``clock`` and ``read`` exist so a test can put fakes where a GPU would be. ``read`` is
    the whole reader the queue calls; left out, the app composes the real one per Reading, inside the
    environment lease (see ``_read_under_lease``).
    """

    def __init__(
        self,
        limits: DemoLimits | None = None,
        engine_name: str | None = None,
        environ: MutableMapping[str, str] | None = None,
        *,
        ledger: GateLedger | None = None,
        read: Reader | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        # The environment is the demo's configuration and, for a leased worker, the place the lease
        # publishes the endpoint ``engine_config`` and the probe are then built from.
        self._environ = environ if environ is not None else os.environ
        self._limits = limits if limits is not None else demo_limits(self._environ)
        self._engine_name = engine_name if engine_name is not None else configured_engine(self._environ)
        self._ledger = ledger if ledger is not None else InMemoryGateLedger(clock=clock, limits=self._limits)
        self._queue = ReadingQueue(read if read is not None else self._read_under_lease, self._limits)

    @property
    def limits(self) -> DemoLimits:
        return self._limits

    @property
    def engine_name(self) -> str:
        return self._engine_name

    @property
    def ledger(self) -> GateLedger:
        return self._ledger

    @property
    def queue(self) -> ReadingQueue:
        return self._queue

    def status(self) -> dict[str, Any]:
        """What the page is allowed to know: the limits, the budget, the depth, and whether OCR is wired.

        Deliberately not the bearer, the base URL or the provider's own settings: a leaked bearer is a
        bill, and the address of a private worker is not a visitor's business (ADR-0012).
        """
        usage = self._ledger.usage(NO_CLIENT)
        return {
            "limits": asdict(self._limits),
            "ocr_pages_remaining": max(self._limits.daily_ocr_page_budget - usage.ocr_pages_today, 0),
            "queue": self._queue.depth(),
            "ocr_endpoint_configured": _endpoint_configured(self._environ),
        }

    def submit(self, content: bytes, client: str, name: str = DEFAULT_UPLOAD_NAME) -> ReadingJob:
        """Queue one upload; ``QueueFull`` is the handler's 503 and nothing else is refused here."""
        return self._queue.submit(content, client, name)

    def job(self, job_id: str) -> ReadingJob | None:
        """One job's standing, or ``None`` for an id this process never issued or no longer keeps."""
        return self._queue.get(job_id)

    def close(self) -> None:
        """Stop the queue's worker; the process is ending or the test is done."""
        self._queue.close()

    def _read_under_lease(
        self, content: bytes, client: str, on_stage: Callable[[ReadingStage], None], name: str
    ) -> ReadingOutcome:
        """One Reading, with the GPU leased for exactly its length.

        The lease mutates the environment and the engine is built from that environment *after* the
        lease, which is why this cannot be built once at startup: a worker this demo rents exists only
        while a Reading is in flight. The single queue slot is what makes that safe (ADR-0012).

        A lease that fails before a Reading happened is a Refusal of the ``unavailable`` kind, because
        the gates did not close and no lane failed -- the demo could not reach its worker. A release
        that fails *after* a good Reading is logged and the Reading is still returned: the visitor's
        text is real, and the operator's own report is the log line (ADR-0007).
        """
        outcome = None
        leaked: BaseException | None = None
        try:
            with leased_environment(self._environ):
                demo_read = self._demo_read()
                outcome = demo_read.read(content, client, on_stage, name)
        except Exception as error:  # the demo answers with the fault instead of crashing
            if outcome is None:
                return self._unavailable(error)
            leaked = error
        if leaked is not None:
            logger.warning("the GPU lease could not be released after a Reading: %s", leaked)
        # The use case answers one of its three outcomes or raises; reaching here means it answered.
        assert outcome is not None
        return outcome

    def _demo_read(self) -> DemoRead:
        """The use case, built from the leased environment the way a run builds its own runtime."""
        values = self._environ
        policy = CurationPolicy(
            engine_name=self._engine_name,
            routing=ocr_routing_policy(values),
        )
        configs: list[EngineConfig] = (
            [] if self._engine_name == NATIVE_ENGINE else [engine_config(values, self._engine_name)]
        )
        return DemoRead(
            probe=PymupdfTextLayerProbe(),
            engines=build_engines(configs),
            policy=policy,
            gates=SpendGatePolicy(self._limits),
            ledger=self._ledger,
            endpoint=HttpOcrEndpointProbe(
                values.get(ENGINE_BASE_URL, ""),
                values.get(ENGINE_API_KEY, ""),
            ),
            warm_timeout_seconds=self._limits.warm_timeout_seconds,
            poll_interval_seconds=self._limits.poll_interval_seconds,
        )

    def _unavailable(self, error: BaseException) -> Refusal:
        """A demo that could not build a reader, said in the refusal vocabulary the page already has."""
        return Refusal(
            kind="unavailable",
            reason=f"the demo could not reach its OCR worker: {_words(error)}",
            plan=None,
            thresholds=ocr_routing_policy(self._environ),
        )


def _number(limit: _Limit, raw: str) -> float | int:
    """One configured limit, or an error that names the variable and the value that was not a number."""
    try:
        value = int(raw) if limit.whole else float(raw)
    except ValueError as error:
        kind = "a whole number" if limit.whole else "a number"
        raise DemoLimitError(f"{limit.variable} is not {kind}: {raw!r}") from error
    if value < 0:
        raise DemoLimitError(f"{limit.variable} cannot be negative: {raw!r}")
    return value


def _endpoint_configured(values: Mapping[str, str]) -> bool:
    """Whether the demo's environment names an OCR worker at all, without leasing one to find out.

    The address may come from the engine's own names (a deployed app an operator pasted in) or from the
    provider's (a worker this demo rents or a fixed server). Neither value is echoed: this is a yes/no.
    """
    return any(
        values.get(variable, "").strip()
        for variable in (ENGINE_BASE_URL, GPU_BASE_URL, GPU_START_COMMAND)
    )


def _words(error: BaseException) -> str:
    return str(error) or type(error).__name__


class DemoRequestHandler(BaseHTTPRequestHandler):
    """Serve the page, the status answer, the upload and the job poll; every path is explicit."""

    app: DemoApp
    server_version = "FinePDFsLiveDemo/1"
    sys_version = ""

    def do_GET(self) -> None:
        self._get(head=False)

    def do_HEAD(self) -> None:
        self._get(head=True)

    def do_POST(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path != "/api/read":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        self._read()

    def log_message(self, format: str, *args: object) -> None:
        """Keep the console quiet; ``serve`` already prints where it is serving."""

    def _get(self, *, head: bool) -> None:
        path = unquote(urlsplit(self.path).path)
        if "\x00" in path or "\\" in path or ".." in path.split("/"):
            self._error(HTTPStatus.NOT_FOUND, "not found", head=head)
            return
        if path in ("/", "/index.html"):
            self._html(DEMO_HTML, head=head)
        elif path == "/api/status":
            self._json(self.app.status(), head=head)
        elif path.startswith("/api/read/"):
            job_id = path.removeprefix("/api/read/")
            job = self.app.job(job_id)
            if job is None:
                self._error(HTTPStatus.NOT_FOUND, f"no job {job_id}", head=head)
                return
            self._json(job.as_json(), head=head)
        else:
            self._error(HTTPStatus.NOT_FOUND, "not found", head=head)

    def _read(self) -> None:
        """Take one upload: refuse what cannot be read, otherwise queue it and answer its job id.

        The body is never read to find out whether it fits. ``Content-Length`` is checked against the
        byte cap first, so the demo answers a ``413`` without pulling a body it has already decided not
        to look at; a body whose length is not declared cannot be bounded, so it is refused too.
        """
        content_type = self.headers.get_content_type()
        if content_type != PDF_MEDIA_TYPE:
            self._refuse(
                HTTPStatus.BAD_REQUEST,
                UPLOAD,
                f"the demo reads {PDF_MEDIA_TYPE}, not {content_type}",
            )
            return
        length = self._declared_length()
        if length is None:
            self._refuse(HTTPStatus.BAD_REQUEST, UPLOAD, "the upload declared no Content-Length")
            return
        if length > self.app.limits.upload_bytes:
            self._refuse(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                UPLOAD,
                f"this upload is {length} bytes; the demo reads at most {self.app.limits.upload_bytes}",
            )
            return
        if length == 0:
            self._refuse(HTTPStatus.BAD_REQUEST, UPLOAD, "the upload is empty")
            return
        content = self.rfile.read(length)
        if len(content) != length:
            self._refuse(
                HTTPStatus.BAD_REQUEST,
                UPLOAD,
                f"the upload was {len(content)} of the {length} bytes it declared",
            )
            return
        try:
            job = self.app.submit(content, _client(self), self._upload_name())
        except QueueFull as full:
            self._refuse(HTTPStatus.SERVICE_UNAVAILABLE, full.kind, full.reason)
            return
        self._json({"job": job.id}, status=HTTPStatus.ACCEPTED)

    def _declared_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            length = int(raw)
        except ValueError:
            return None
        return length if length >= 0 else None

    def _upload_name(self) -> str:
        """The visitor's own file name, or a neutral one; it names a record and never a path."""
        name = self.headers.get(UPLOAD_NAME_HEADER, "").strip()
        return name or DEFAULT_UPLOAD_NAME

    def _json(self, payload: Any, *, status: HTTPStatus = HTTPStatus.OK, head: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _html(self, body: str, *, head: bool) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._headers("text/html; charset=utf-8", len(encoded))
        self.end_headers()
        if not head:
            self.wfile.write(encoded)

    def _refuse(self, status: HTTPStatus, kind: str, reason: str) -> None:
        """A refusal, in the one shape every refused answer has: its kind and the visitor's sentence."""
        self._json({"outcome": "refused", "refusal": {"kind": kind, "reason": reason}}, status=status)

    def _error(self, status: HTTPStatus, message: str, *, head: bool = False) -> None:
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")


def _client(handler: BaseHTTPRequestHandler) -> str:
    """The client the rate limit counts against: the left-most forwarded hop, else the peer.

    The left-most hop is the address the proxy says the request came from; anything after it is a hop
    the proxy added. Without the header -- a demo reached directly -- the peer address is the client.
    This is a spend brake, not an identity (ADR-0012): a public deployment must sit behind a proxy that
    sets the header.
    """
    forwarded = handler.headers.get("X-Forwarded-For", "")
    first = forwarded.split(",")[0].strip()
    return first or handler.client_address[0]


def make_server(app: DemoApp, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """A server bound to ``host:port``; port 0 asks the OS for a free port (used by tests)."""
    handler = type("BoundDemoHandler", (DemoRequestHandler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def build_app(values: Mapping[str, str] | None = None) -> DemoApp:
    """The composition root from a mapping: ``os.environ`` by default, a test's mapping otherwise."""
    environ: MutableMapping[str, str] = os.environ if values is None else dict(values)
    return DemoApp(environ=environ)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, values: Mapping[str, str] | None = None) -> None:
    """Serve the demo until interrupted. The address printed is the one a visitor can open."""
    app = build_app(values)
    server = make_server(app, host, port)
    print(f"serving the live upload demo at http://{host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.close()


def build_parser() -> argparse.ArgumentParser:
    """The console script's flags: where to bind, and an override for every demo limit."""
    parser = argparse.ArgumentParser(
        prog="finepdfs-live-demo",
        description="Serve the live upload demo: one PDF in, its OCR Decision and the page text out.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"address to bind (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to bind (default {DEFAULT_PORT})")
    for field, flag in _FLAGS.items():
        limit = _LIMITS[field]
        parser.add_argument(
            flag,
            dest=field,
            type=int if limit.whole else float,
            help=f"{limit.noun} (default {limit.default:g})",
        )
    return parser


def _environment_from_args(args: argparse.Namespace) -> dict[str, str]:
    """The process environment with the flags written over it: a flag only changes what it names."""
    values = dict(os.environ)
    for field in _FLAGS:
        configured = getattr(args, field, None)
        if configured is not None:
            values[_LIMITS[field].variable] = str(configured)
    return values


def main(argv: list[str] | None = None) -> int:
    """Serve the demo as the console script does, with the environment read at startup."""
    args = build_parser().parse_args(argv)
    try:
        serve(args.host, args.port, _environment_from_args(args))
    except (DemoLimitError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - the console script's own entry
    raise SystemExit(main())
