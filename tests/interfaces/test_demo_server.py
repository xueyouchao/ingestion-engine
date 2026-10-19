"""The live demo through its HTTP API: upload a PDF, poll the job, see the decision and the text.

The real ``ThreadingHTTPServer`` runs on port 0. What stands in for the GPU is a fake probe, fake
engines, a fake endpoint probe and an in-memory Gate Ledger, composed into the real ``DemoRead`` -- so
the gate order, the warming stage and the lane choice are the ones the demo actually runs, not a mock of
them. The one test that uses the real composition reads a text-layer PDF from the fixtures with no
network and no model, and checks that nothing was written.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from finepdfs.application.curate import CurationPolicy, RoutingPlan
from finepdfs.application.demo_read import DemoRead, Reading
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
    SpendGatePolicy,
)
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
from finepdfs.domain.ocr import OcrRoutingPolicy, RoutingDecision
from finepdfs.infrastructure.demo.ledger import InMemoryGateLedger
from finepdfs.infrastructure.engines.environment import engine_adapters
from finepdfs.interfaces import demo_page, demo_server
from finepdfs.interfaces.demo_server import DemoApp

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TEXT_LAYER_PDF = (FIXTURES / "warc_payload.bin").read_bytes()
OCR_ENGINE = "paddleocr-vl"
TOKEN = "s3cret-bearer-nobody-should-see"
WORKER = "https://worker.example.invalid/v1"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

HEALTHY = TextLayerQuality(
    page_count=2, pages_with_text=2, character_count=4000, garbled_ratio=0.0, image_dominated_pages=0
)
SCANNED = TextLayerQuality(
    page_count=2, pages_with_text=0, character_count=0, garbled_ratio=0.0, image_dominated_pages=2
)
OVER_THE_PAGE_CAP = TextLayerQuality(
    page_count=30, pages_with_text=0, character_count=0, garbled_ratio=0.0, image_dominated_pages=30
)


class FakeProbe:
    """A text-layer probe that reports the quality it was handed and tells the test it was asked."""

    def __init__(self, quality: TextLayerQuality) -> None:
        self._quality = quality
        self.measured: list[bytes] = []

    def measure(self, content: bytes) -> TextLayerQuality:
        self.measured.append(content)
        return self._quality


class FakeEngine:
    """An engine that answers fixed pages and counts that it was asked at all."""

    def __init__(self, name: str, pages: list[PageText], detail: str = "") -> None:
        self._identity = EngineIdentity(name=name, detail=detail)
        self._pages = pages
        self.calls = 0

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document) -> ExtractedDocument:
        self.calls += 1
        return ExtractedDocument(pages=tuple(self._pages), engine=self._identity)


class FakeEndpoint:
    """An OCR endpoint that answers a script, and can hold the warming stage open for the test."""

    def __init__(self, answers: list[bool] | None = None, hold: threading.Event | None = None) -> None:
        self._answers = list(answers or [True])
        self._hold = hold
        self.calls = 0

    def ready(self) -> bool:
        self.calls += 1
        if self.calls > 1 and self._hold is not None:
            assert self._hold.wait(5), "the test never released the warming worker"
        index = min(self.calls - 1, len(self._answers) - 1)
        return self._answers[index]


class FakeTime:
    """Hand-driven time, so a warming wait is a number the test decides and not a wall clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class RecordingReader:
    """A reader that records what reached it, for the uploads the server must refuse first."""

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def __call__(self, content, client, on_stage, name):
        self.calls.append(content)
        on_stage("routing")
        on_stage("done")
        return _reading_outcome()


class BlockingReader:
    """A reader held open until the test releases it, so a submission meets a busy queue."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, content, client, on_stage, name):
        on_stage("routing")
        self.started.set()
        assert self.release.wait(5), "the test never released the reading"
        return _reading_outcome()


def _reading_outcome() -> Reading:
    """A Reading for the readers that only need the server to see one, not a lane's real work."""
    return Reading(
        plan=RoutingPlan(
            quality=HEALTHY,
            decision=RoutingDecision(False, "the text layer is healthy", {"page_count": 2.0}),
            engine_name="pymupdf",
        ),
        thresholds=OcrRoutingPolicy(),
        engine=EngineIdentity("pymupdf", "fake"),
        pages=(PageText(1, "one"), PageText(2, "two")),
        elapsed_ms=1,
        warm_ms=None,
    )


def _native_engine() -> FakeEngine:
    return FakeEngine("pymupdf", [PageText(1, "alpha"), PageText(2, "beta")], detail="fake")


def _ocr_engine() -> FakeEngine:
    return FakeEngine(OCR_ENGINE, [PageText(1, "ocr one"), PageText(2, "ocr two")], detail="weights-c5630abae1d9")


def _app(
    probe: FakeProbe,
    endpoint: FakeEndpoint,
    limits: DemoLimits | None = None,
    *,
    ledger: InMemoryGateLedger | None = None,
    native: FakeEngine | None = None,
    ocr: FakeEngine | None = None,
    time_source: FakeTime | None = None,
    environ: dict[str, str] | None = None,
) -> DemoApp:
    """The real use case over fakes, composed into the server exactly as the server composes it."""
    settings = limits if limits is not None else DemoLimits()
    time_source = time_source or FakeTime()
    ledger = ledger or InMemoryGateLedger(clock=lambda: NOW, limits=settings)
    read = DemoRead(
        probe=probe,
        engines={"pymupdf": native or _native_engine(), OCR_ENGINE: ocr or _ocr_engine()},
        policy=CurationPolicy(engine_name=OCR_ENGINE, routing=OcrRoutingPolicy()),
        gates=SpendGatePolicy(settings),
        ledger=ledger,
        endpoint=endpoint,
        warm_timeout_seconds=settings.warm_timeout_seconds,
        poll_interval_seconds=settings.poll_interval_seconds,
        clock=time_source.clock,
        sleep=time_source.sleep,
    ).read
    return DemoApp(
        limits=settings,
        engine_name=OCR_ENGINE,
        environ=environ if environ is not None else {},
        ledger=ledger,
        read=read,
    )


@contextmanager
def _serving(app: DemoApp):
    server = demo_server.make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        app.close()


def _get(url: str):
    return urllib.request.urlopen(url)


def _json(url: str) -> dict:
    with urllib.request.urlopen(url) as response:
        assert response.headers["Content-Type"].startswith("application/json")
        return json.loads(response.read())


def _post(base: str, body: bytes, *, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + "/api/read",
        data=body,
        method="POST",
        headers={"Content-Type": "application/pdf", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("the demo never reached the expected state")


def _wait_for_job(base: str, job_id: str) -> dict:
    answer: dict = {}

    def finished() -> bool:
        answer.update(_json(f"{base}/api/read/{job_id}"))
        return answer["stage"] == "done"

    _wait_until(finished)
    return answer


def _upload(base: str, body: bytes = b"%PDF-1.7 uploaded", **headers: str) -> dict:
    status, answer = _post(base, body, headers=headers)
    assert status == 202, answer
    assert set(answer) == {"job"}
    return _wait_for_job(base, answer["job"])


def test_the_page_states_where_the_bytes_go_and_what_is_kept_before_the_upload() -> None:
    app = _app(
        FakeProbe(HEALTHY),
        FakeEndpoint(),
        environ={"FINEPDFS_ENGINE_API_KEY": TOKEN, "FINEPDFS_ENGINE_BASE_URL": WORKER},
    )
    with _serving(app) as base:
        page = _get(base + "/")
        html = page.read().decode("utf-8")

    assert page.status == 200
    assert page.headers["Content-Type"].startswith("text/html")
    statements = html.index('id="statements"')
    limits = html.index('id="limits"')
    upload = html.index('id="upload-file"')
    assert statements < limits < upload, "the limits and the two statements come before the upload control"
    # The ticket lists the read's parts in this order: the decision, the text it read, then the identity
    # that produced it -- a visitor reads the answer before the provenance.
    assert html.index('id="decision"') < html.index('id="pages"') < html.index('id="engine-identity"')
    assert "rented GPU" in html
    assert "nothing is kept" in html
    for identifier in (
        'id="timeline"',
        'id="decision"',
        'id="engine-identity"',
        'id="pages"',
        'id="refusal"',
        'id="failure"',
        'id="warm-measured"',
    ):
        assert identifier in html
    assert "/api/read" in html and "/api/status" in html
    for contract_key in ("warmed_ms", "refusal", "failure", "reading", "thresholds", "features"):
        assert contract_key in html
    assert TOKEN not in html, "the page never carries the bearer"
    assert "worker.example" not in html, "the page never carries the worker's address"


def test_the_page_asset_is_its_own_module() -> None:
    with _serving(_app(FakeProbe(HEALTHY), FakeEndpoint())) as base:
        assert _get(base + "/").read().decode("utf-8") == demo_page.DEMO_HTML

    assert demo_server.DEMO_HTML is demo_page.DEMO_HTML


def test_the_status_answer_carries_the_limits_the_budget_and_the_depth_but_no_secret() -> None:
    settings = replace(DemoLimits(), page_cap=7, queue_depth=0)
    ledger = InMemoryGateLedger(clock=lambda: NOW, limits=settings)
    ledger.charge_ocr_pages(10)
    app = _app(
        FakeProbe(HEALTHY),
        FakeEndpoint(),
        settings,
        ledger=ledger,
        environ={"FINEPDFS_ENGINE_BASE_URL": WORKER, "FINEPDFS_ENGINE_API_KEY": TOKEN},
    )
    with _serving(app) as base:
        raw = _get(base + "/api/status").read().decode("utf-8")
        answer = json.loads(raw)

    assert answer["limits"] == asdict(settings)
    assert answer["limits"]["page_cap"] == 7
    assert answer["ocr_pages_remaining"] == settings.daily_ocr_page_budget - 10
    assert answer["queue"] == {"active": 0, "waiting": 0}
    assert answer["ocr_endpoint_configured"] is True
    assert TOKEN not in raw, "the status answer never carries the bearer"
    assert "worker.example" not in raw, "the status answer never carries the worker's address"


def test_the_status_answer_says_when_no_ocr_endpoint_is_configured() -> None:
    with _serving(_app(FakeProbe(HEALTHY), FakeEndpoint(), environ={})) as base:
        answer = _json(base + "/api/status")

    assert answer["ocr_endpoint_configured"] is False


def test_a_native_upload_is_read_without_touching_the_ocr_lane() -> None:
    native, ocr, endpoint = _native_engine(), _ocr_engine(), FakeEndpoint()
    app = _app(FakeProbe(HEALTHY), endpoint, native=native, ocr=ocr)
    with _serving(app) as base:
        answer = _upload(base, b"%PDF-1.7 a native upload")

    assert answer["stage"] == "done"
    assert answer["outcome"] == "read"
    assert answer["refusal"] is None
    assert answer["failure"] is None
    reading = answer["reading"]
    assert reading["lane"] == "text-layer"
    assert reading["reason"] == "the text layer is healthy: 2000 characters per page across 100% of pages"
    assert reading["engine"] == "pymupdf@fake"
    assert reading["pages"] == [{"number": 1, "text": "alpha"}, {"number": 2, "text": "beta"}]
    assert reading["page_count"] == 2
    assert reading["character_count"] == 9
    assert set(reading["features"]) == {
        "page_count",
        "characters_per_page",
        "text_coverage",
        "garbled_ratio",
        "image_dominated_share",
    }
    assert reading["thresholds"] == {
        "min_characters_per_page": 200.0,
        "min_text_coverage": 0.5,
        "max_garbled_ratio": 0.05,
        "max_image_dominated_share": 0.5,
    }
    assert ocr.calls == 0, "a native read never reaches the OCR engine"
    assert endpoint.calls == 0, "a native read never asks the OCR worker whether it is awake"


def test_an_ocr_upload_shows_the_warming_stage_and_the_measured_wait() -> None:
    hold = threading.Event()
    app = _app(FakeProbe(SCANNED), FakeEndpoint([False, True], hold=hold), ocr=_ocr_engine())
    with _serving(app) as base:
        status, submitted = _post(base, b"%PDF-1.7 a scanned upload")
        assert status == 202
        job_id = submitted["job"]

        _wait_until(lambda: _json(f"{base}/api/read/{job_id}")["stage"] == "warming")
        warming = _json(f"{base}/api/read/{job_id}")
        assert warming["outcome"] is None, "a stage, not an outcome, while the worker is cold"

        hold.set()
        answer = _wait_for_job(base, job_id)

    assert answer["outcome"] == "read"
    assert answer["warmed_ms"] == 2000, "the warm-up is the wait that was measured, not a guess"
    assert answer["elapsed_ms"] is not None
    assert answer["reading"]["lane"] == "ocr"
    assert answer["reading"]["engine"] == f"{OCR_ENGINE}@weights-c5630abae1d9"
    assert [page["text"] for page in answer["reading"]["pages"]] == ["ocr one", "ocr two"]


@pytest.mark.parametrize(
    ("quality", "usage", "client", "expected_kind"),
    [
        (OVER_THE_PAGE_CAP, 0, "203.0.113.7", "page-cap"),
        (HEALTHY, DEFAULT_RATE_LIMIT_READS, "198.51.100.9", "rate-limit"),
        (SCANNED, 0, "203.0.113.7", "daily-budget"),
    ],
)
def test_each_refusal_kind_comes_back_from_its_own_gate(
    quality: TextLayerQuality, usage: int, client: str, expected_kind: str
) -> None:
    settings = DemoLimits()
    ledger = InMemoryGateLedger(clock=lambda: NOW, limits=settings)
    if expected_kind == "rate-limit":
        for _ in range(usage):
            ledger.record_read(client)
    if expected_kind == "daily-budget":
        ledger.charge_ocr_pages(DEFAULT_DAILY_OCR_PAGE_BUDGET)
    app = _app(FakeProbe(quality), FakeEndpoint(), settings, ledger=ledger)
    headers = {"X-Forwarded-For": "198.51.100.9, 10.0.0.1"} if expected_kind == "rate-limit" else {}
    with _serving(app) as base:
        answer = _upload(base, b"%PDF-1.7 refused", **headers)

    assert answer["outcome"] == "refused"
    assert answer["refusal"]["kind"] == expected_kind
    assert answer["refusal"]["reason"], "a refusal names the gate that closed in words"
    assert answer["reading"] is None
    assert answer["failure"] is None


def test_an_upload_over_the_byte_cap_is_a_413_before_the_body_is_read() -> None:
    reader = RecordingReader()
    app = DemoApp(limits=replace(DemoLimits(), upload_bytes=64), environ={}, read=reader)
    with _serving(app) as base:
        parts = urlsplit(base)
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        try:
            connection.putrequest("POST", "/api/read")
            connection.putheader("Content-Type", "application/pdf")
            connection.putheader("Content-Length", "6400")
            connection.endheaders()
            connection.send(b"x" * 10)  # far less than declared: reading to the cap would block
            response = connection.getresponse()
            status, answer = response.status, json.loads(response.read())
        finally:
            connection.close()

    assert status == 413
    assert answer["outcome"] == "refused"
    assert answer["refusal"]["kind"] == "upload"
    assert "64" in answer["refusal"]["reason"]
    assert reader.calls == [], "the upload is refused before a Reading is started"


def test_an_empty_upload_or_a_body_that_is_not_a_pdf_is_a_400() -> None:
    reader = RecordingReader()
    app = DemoApp(limits=DemoLimits(), environ={}, read=reader)
    with _serving(app) as base:
        empty_status, empty = _post(base, b"")
        type_status, wrong_type = _post(base, b"%PDF-1.7", headers={"Content-Type": "text/plain"})

    assert empty_status == 400
    assert empty["refusal"]["kind"] == "upload"
    assert "empty" in empty["refusal"]["reason"]
    assert type_status == 400
    assert wrong_type["refusal"]["kind"] == "upload"
    assert "text/plain" in wrong_type["refusal"]["reason"]
    assert reader.calls == [], "a body that is not a PDF never becomes a Reading"


def test_a_second_upload_is_refused_while_one_is_reading() -> None:
    reader = BlockingReader()
    app = DemoApp(limits=replace(DemoLimits(), queue_depth=0), environ={}, read=reader)
    with _serving(app) as base:
        first_status, first = _post(base, b"%PDF-1.7 first")
        assert first_status == 202
        assert reader.started.wait(5)

        second_status, second = _post(base, b"%PDF-1.7 second")

        assert second_status == 503
        assert second["outcome"] == "refused"
        assert second["refusal"]["kind"] == "busy"
        assert second["refusal"]["reason"]
        reader.release.set()
        answer = _wait_for_job(base, first["job"])

    assert answer["outcome"] == "read"


def test_an_unknown_job_id_is_a_404() -> None:
    with (
        _serving(_app(FakeProbe(HEALTHY), FakeEndpoint())) as base,
        pytest.raises(urllib.error.HTTPError) as caught,
    ):
        _get(base + "/api/read/0123456789abcdef")

    assert caught.value.code == 404


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_serving_a_reading_writes_nothing(tmp_path, monkeypatch) -> None:
    work = tmp_path / "work"
    spool = tmp_path / "tmp"
    work.mkdir()
    spool.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("TMPDIR", str(spool))
    # The real composition root: the pymupdf probe, the native engine, no provider and no network.
    app = DemoApp(environ={})
    before = {root: _snapshot(root) for root in (work, spool)}
    try:
        with _serving(app) as base:
            answer = _upload(base, TEXT_LAYER_PDF, **{"X-Upload-Name": "visitor.pdf"})
    finally:
        app.close()

    assert answer["outcome"] == "read"
    assert answer["reading"]["lane"] == "text-layer"
    assert answer["reading"]["page_count"] == 3
    assert {root: _snapshot(root) for root in (work, spool)} == before, "the demo keeps no uploaded PDF, page or text"


def test_the_demo_limits_default_to_the_specs_numbers_and_read_the_environment() -> None:
    assert demo_server.demo_limits({}) == DemoLimits()
    configured = demo_server.demo_limits(
        {
            "FINEPDFS_DEMO_PAGE_CAP": "7",
            "FINEPDFS_DEMO_RATE_LIMIT_READS": "9",
            "FINEPDFS_DEMO_RATE_LIMIT_WINDOW_SECONDS": "60",
            "FINEPDFS_DEMO_DAILY_OCR_PAGES": "11",
            "FINEPDFS_DEMO_UPLOAD_BYTES": "1024",
            "FINEPDFS_DEMO_QUEUE_DEPTH": "0",
            "FINEPDFS_DEMO_WARM_TIMEOUT_SECONDS": "30",
            "FINEPDFS_DEMO_POLL_INTERVAL_SECONDS": "0.5",
        }
    )
    assert configured == DemoLimits(
        page_cap=7,
        rate_limit_reads=9,
        rate_limit_window_seconds=60.0,
        daily_ocr_page_budget=11,
        upload_bytes=1024,
        queue_depth=0,
        warm_timeout_seconds=30.0,
        poll_interval_seconds=0.5,
    )
    assert (DEFAULT_PAGE_CAP, DEFAULT_RATE_LIMIT_READS, DEFAULT_RATE_LIMIT_WINDOW_SECONDS) == (25, 5, 600)
    assert (DEFAULT_DAILY_OCR_PAGE_BUDGET, DEFAULT_UPLOAD_BYTES, DEFAULT_QUEUE_DEPTH) == (300, 20 * 1024 * 1024, 3)
    assert (DEFAULT_WARM_TIMEOUT_SECONDS, DEFAULT_POLL_INTERVAL_SECONDS) == (900, 2)


def test_a_mistyped_limit_names_the_variable_it_came_from() -> None:
    with pytest.raises(ValueError, match="FINEPDFS_DEMO_PAGE_CAP"):
        demo_server.demo_limits({"FINEPDFS_DEMO_PAGE_CAP": "twenty-five"})


def test_the_console_script_serves_loopback_8766_by_default(monkeypatch) -> None:
    served: list[tuple[str, int, dict]] = []
    monkeypatch.setattr(demo_server, "serve", lambda host, port, values: served.append((host, port, dict(values))))
    # The defaults are the process having nothing set, whatever the suite's other tests exported before.
    for variable in (demo_server.ENGINE, "FINEPDFS_DEMO_PAGE_CAP"):
        monkeypatch.delenv(variable, raising=False)

    assert demo_server.main([]) == 0

    host, port, values = served[-1]
    assert (host, port) == ("127.0.0.1", 8766)
    assert demo_server.demo_limits(values) == DemoLimits()
    assert demo_server.configured_engine(values) == OCR_ENGINE, "the demo's OCR lane defaults to the deployed engine"


def test_the_console_script_takes_host_port_and_the_limit_overrides(monkeypatch) -> None:
    served: list[tuple[str, int, dict]] = []
    monkeypatch.setattr(demo_server, "serve", lambda host, port, values: served.append((host, port, dict(values))))

    assert demo_server.main(["--host", "0.0.0.0", "--port", "9000", "--page-cap", "7", "--upload-bytes", "1024"]) == 0

    host, port, values = served[-1]
    assert (host, port) == ("0.0.0.0", 9000)
    limits = demo_server.demo_limits(values)
    assert (limits.page_cap, limits.upload_bytes) == (7, 1024)
    assert limits.rate_limit_reads == DEFAULT_RATE_LIMIT_READS, "a flag only changes the limit it names"


def test_every_limit_flag_reaches_the_limit_it_names() -> None:
    # Written as a loop on purpose: three of the eight flags do not spell their field's name
    # (--daily-ocr-pages sets daily_ocr_page_budget), and a hand-written list of flags is exactly how one
    # of them went dead while the parser still accepted it.
    for field, flag in demo_server._FLAGS.items():
        args = demo_server.build_parser().parse_args([flag, "3"])
        values = demo_server._environment_from_args(args)

        assert float(values[demo_server._LIMITS[field].variable]) == 3, f"{flag} did not reach {field}"
        changed = [
            other
            for other in demo_server._LIMITS
            if other != field and getattr(demo_server.demo_limits(values), other) != getattr(DemoLimits(), other)
        ]
        assert changed == [], f"{flag} also changed {changed}"


def test_the_limit_overrides_beat_the_environment(monkeypatch) -> None:
    served: list[tuple[str, int, dict]] = []
    monkeypatch.setattr(demo_server, "serve", lambda host, port, values: served.append((host, port, dict(values))))
    monkeypatch.setenv("FINEPDFS_DEMO_PAGE_CAP", "9")
    monkeypatch.setenv("FINEPDFS_DEMO_QUEUE_DEPTH", "1")

    assert demo_server.main(["--page-cap", "4"]) == 0

    limits = demo_server.demo_limits(served[-1][2])
    assert limits.page_cap == 4
    assert limits.queue_depth == 1, "the environment still supplies what no flag overrode"


def test_the_demo_composes_its_engine_environment_the_way_a_run_does() -> None:
    """The demo's engines and thresholds are the shared assembler's output, not a second copy of them.

    The identity half of the agreement is pinned in tests/infrastructure/test_engine_environment.py,
    where a run's engines are compared with the assembler's. Here the demo's half is pinned: the
    thresholds it applies and the address its readiness probe asks are the environment's own values.
    """
    values = {
        "FINEPDFS_ENGINE": "paddleocr-vl",
        "FINEPDFS_ENGINE_BASE_URL": "https://ocr.example/v1",
        "FINEPDFS_ENGINE_MODEL": "PaddleOCR-VL-1.6",
        "FINEPDFS_ENGINE_API_KEY": "secret-token",
        "FINEPDFS_ENGINE_DETAIL": "modal:ocr:1.6",
        "FINEPDFS_ENGINE_DPI": "300",
        "FINEPDFS_MIN_CHARACTERS_PER_PAGE": "120",
        "FINEPDFS_MIN_TEXT_COVERAGE": "0.4",
        "FINEPDFS_MAX_GARBLED_RATIO": "0.02",
    }
    app = DemoApp(environ=dict(values))
    try:
        demo = app._demo_read()
    finally:
        app.close()

    assert demo.thresholds == engine_adapters(values, "paddleocr-vl").routing
    assert demo.thresholds.min_characters_per_page == 120.0
    assert demo.endpoint.base_url == "https://ocr.example/v1"


def test_the_engine_is_named_by_the_run_variable_not_by_a_demo_copy() -> None:
    assert demo_server.configured_engine({"FINEPDFS_ENGINE": "docling"}) == "docling"
    assert demo_server.configured_engine({"FINEPDFS_ENGINE": ""}) == OCR_ENGINE
