"""The demo's one use case, driven through ports with hand-written fakes.

Like ``test_curate_document.py``, this is a seam: no network, no models, no filesystem, and hand time
driven by a fake so a fifteen-minute warm timeout is an observation. What is pinned is what a visitor
sees -- a Reading, a Refusal naming its gate, a Failed Reading in the error's own words -- plus the two
promises the demo makes about cost: a refused read records nothing, and a native read never reaches the
OCR engine.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf

from finepdfs.application.curate import NATIVE_TEXT_ENGINE, CurationPolicy, RoutingPlan
from finepdfs.application.demo_read import DemoRead, FailedReading, Reading, Refusal
from finepdfs.domain.demo import DemoLimits, SpendGatePolicy, UsageSnapshot
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.domain.ports import OcrEndpointUnavailable
from finepdfs.infrastructure.engines.paddleocr_vl import PaddleOcrVlCliEngine
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextLayerProbe
from finepdfs.infrastructure.engines.subprocess_support import CommandResult

OCR_ENGINE = "paddleocr-vl"

HEALTHY = TextLayerQuality(
    page_count=2, pages_with_text=2, character_count=4000, garbled_ratio=0.0, image_dominated_pages=0
)
SCANNED = TextLayerQuality(
    page_count=2, pages_with_text=0, character_count=0, garbled_ratio=0.0, image_dominated_pages=2
)


class FakeProbe:
    """Measures whatever quality it was handed, or raises the failure it was handed."""

    def __init__(self, quality: TextLayerQuality, failure: Exception | None = None) -> None:
        self._quality = quality
        self._failure = failure
        self.measured: list[bytes] = []

    def measure(self, content: bytes) -> TextLayerQuality:
        self.measured.append(content)
        if self._failure is not None:
            raise self._failure
        return self._quality


class FakeEngine:
    """An OCR Engine that records that it was asked, and can spend fake time doing it."""

    def __init__(
        self,
        name: str,
        pages: list[PageText],
        detail: str = "",
        failure: Exception | None = None,
        spends_seconds: float = 0.0,
        spends: Callable[[float], None] | None = None,
    ) -> None:
        self._identity = EngineIdentity(name=name, detail=detail)
        self._pages = pages
        self._failure = failure
        self._spends_seconds = spends_seconds
        self._spends = spends
        self.calls = 0

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document) -> ExtractedDocument:
        self.calls += 1
        if self._spends is not None:
            self._spends(self._spends_seconds)
        if self._failure is not None:
            raise self._failure
        return ExtractedDocument(pages=tuple(self._pages), engine=self._identity)


class FakeLedger:
    """The Gate Ledger's counters, as a test can read them back."""

    def __init__(self, snapshot: UsageSnapshot | None = None) -> None:
        self._snapshot = snapshot or UsageSnapshot()
        self.asked: list[str] = []
        self.reads: list[str] = []
        self.charged = 0

    def usage(self, client: str) -> UsageSnapshot:
        self.asked.append(client)
        return self._snapshot

    def record_read(self, client: str) -> None:
        self.reads.append(client)

    def charge_ocr_pages(self, pages: int) -> None:
        self.charged += pages


class FakeEndpoint:
    """An OCR endpoint that answers a script one readiness question per call."""

    def __init__(self, answers: list[bool] | None = None, failure: Exception | None = None) -> None:
        self._answers = list(answers or [True])
        self._failure = failure
        self.calls = 0

    def ready(self) -> bool:
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        index = min(self.calls - 1, len(self._answers) - 1)
        return self._answers[index]


class _SlowFirstAnswer:
    """An endpoint whose first answer takes as long as the probe waits: the request that starts a container."""

    def __init__(self, time: FakeTime, seconds: float = 8.0) -> None:
        self._time = time
        self._seconds = seconds
        self.calls = 0

    def ready(self) -> bool:
        self.calls += 1
        self._time.advance(self._seconds)
        return True


class FakeTime:
    """The only time a demo read in this file sees: a clock a sleep moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Fakes:
    """Everything a test drives the use case through, so its assertions stay on the outcome."""

    def __init__(self, quality: TextLayerQuality, **options: Any) -> None:
        self.time = FakeTime()
        self.probe = FakeProbe(quality, options.get("probe_failure"))
        self.native = FakeEngine(
            NATIVE_TEXT_ENGINE,
            options.get("native_pages", _pages("native")),
            failure=options.get("native_failure"),
            spends_seconds=options.get("native_seconds", 0.0),
            spends=self.time.advance,
        )
        self.ocr = FakeEngine(
            OCR_ENGINE,
            options.get("ocr_pages", _pages("ocr")),
            detail="paddleocr-vl-1.6-weights-c5630abae1d9",
            failure=options.get("ocr_failure"),
        )
        self.ledger = FakeLedger(options.get("usage"))
        self.endpoint = FakeEndpoint(options.get("answers"), options.get("endpoint_failure"))
        # The OCR engine is reachable only through the worker, which is the point: a native read, or a
        # refusal, has no way to touch it, because nothing builds it until a read says it needs one.
        self.worker = FakeWorker(
            self.ocr,
            self.endpoint,
            enter_failure=options.get("worker_failure"),
            release_failure=options.get("release_failure"),
        )
        self.engines = {NATIVE_TEXT_ENGINE: self.native}

    def use_case(self, **overrides: Any) -> DemoRead:
        routing = overrides.pop("routing", OcrRoutingPolicy())
        limits = overrides.pop("limits", DemoLimits())
        return DemoRead(
            probe=self.probe,
            engines=self.engines,
            policy=CurationPolicy(engine_name=OCR_ENGINE, routing=routing),
            gates=SpendGatePolicy(limits),
            ledger=self.ledger,
            worker=self.worker,
            warm_timeout_seconds=overrides.pop("warm_timeout_seconds", 900.0),
            poll_interval_seconds=overrides.pop("poll_interval_seconds", 2.0),
            clock=self.time.clock,
            sleep=self.time.sleep,
            redact=overrides.pop("redact", str),
        )


class FakeWorker:
    """The OCR lane's worker: entering it yields the engine and its endpoint, and it says so out loud.

    ``entered`` is what the tests read to prove a read did *not* rent a worker: a native upload and a
    refused one must leave it at zero, because that is the difference between a demo and a bill.
    """

    def __init__(
        self,
        engine: FakeEngine,
        endpoint: FakeEndpoint,
        enter_failure: Exception | None = None,
        release_failure: Exception | None = None,
    ) -> None:
        self.engine = engine
        self.endpoint = endpoint
        self._enter_failure = enter_failure
        self._release_failure = release_failure
        self.entered = 0
        self.exited = 0

    def __call__(self) -> FakeWorker:
        return self

    def __enter__(self) -> tuple[FakeEngine, FakeEndpoint]:
        self.entered += 1
        if self._enter_failure is not None:
            raise self._enter_failure
        return self.engine, self.endpoint

    def __exit__(self, *exc_info: object) -> None:
        self.exited += 1
        if self._release_failure is not None:
            raise self._release_failure


def _pages(prefix: str) -> list[PageText]:
    return [PageText(number=number, text=f"{prefix} text on page {number}\n") for number in (1, 2)]


def _demo(quality: TextLayerQuality = HEALTHY, **options: Any) -> tuple[DemoRead, Fakes]:
    fakes = Fakes(quality, **options)
    return fakes.use_case(**options), fakes


def _stages() -> tuple[list[str], Callable[[str], None]]:
    seen: list[str] = []
    return seen, seen.append


def test_a_healthy_upload_is_read_by_the_text_layer_and_never_touches_the_ocr_engine() -> None:
    demo, fakes = _demo(HEALTHY)

    outcome = demo.read(b"%PDF-1.7 healthy bytes", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert [page.text for page in outcome.pages] == [page.text for page in _pages("native")]
    assert [page.number for page in outcome.pages] == [1, 2]
    assert str(outcome.engine) == NATIVE_TEXT_ENGINE
    assert outcome.plan.engine_name == NATIVE_TEXT_ENGINE
    assert outcome.plan.decision.needs_ocr is False
    assert fakes.ocr.calls == 0
    assert fakes.endpoint.calls == 0


def test_the_routing_decision_travels_back_with_its_features_and_its_thresholds() -> None:
    demo, fakes = _demo(SCANNED)

    outcome = demo.read(b"%PDF-1.7 scanned bytes", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert isinstance(outcome.plan, RoutingPlan)
    assert outcome.plan.quality == SCANNED
    assert outcome.plan.decision.features == {
        "page_count": 2.0,
        "characters_per_page": 0.0,
        "text_coverage": 0.0,
        "garbled_ratio": 0.0,
        "image_dominated_share": 1.0,
    }
    assert outcome.plan.decision.reason == "only 0% of pages carry text, below the 50% floor"
    assert outcome.thresholds == OcrRoutingPolicy()
    assert outcome.plan.engine_name == OCR_ENGINE
    assert outcome.engine == fakes.ocr.identity


def test_the_routing_stage_is_announced_first_and_done_last() -> None:
    demo, _ = _demo(HEALTHY)
    seen, on_stage = _stages()

    demo.read(b"%PDF", "10.0.0.1", on_stage)

    assert seen == ["routing", "reading", "done"]


def test_the_elapsed_time_is_measured_and_reported_in_milliseconds() -> None:
    demo, _ = _demo(HEALTHY, native_seconds=0.5)

    outcome = demo.read(b"%PDF", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert outcome.elapsed_ms == 500
    assert outcome.warm_ms is None


def test_an_upload_over_the_page_cap_is_refused_before_anything_is_spent() -> None:
    demo, fakes = _demo(HEALTHY, limits=DemoLimits(page_cap=1))
    seen, on_stage = _stages()

    outcome = demo.read(b"%PDF", "10.0.0.1", on_stage)

    assert isinstance(outcome, Refusal)
    assert outcome.kind == "page-cap"
    assert outcome.reason == "this PDF has 2 pages; the demo reads up to 1"
    assert outcome.plan is not None and outcome.plan.decision.needs_ocr is False
    assert seen == ["routing"]
    assert fakes.ledger.reads == []
    assert fakes.ledger.charged == 0
    assert fakes.native.calls == 0


def test_the_rate_limit_closes_before_the_daily_ocr_budget() -> None:
    demo, fakes = _demo(
        SCANNED,
        usage=UsageSnapshot(reads_in_window=5, ocr_pages_today=300),
        limits=DemoLimits(daily_ocr_page_budget=300),
    )

    outcome = demo.read(b"%PDF", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Refusal)
    assert outcome.kind == "rate-limit"
    assert outcome.reason == "you have read 5 documents in the last 10 minutes; the demo allows 5"
    assert fakes.ledger.asked == ["10.0.0.1"]
    assert fakes.ledger.reads == []
    assert fakes.ledger.charged == 0
    assert fakes.ocr.calls == 0


def test_the_daily_ocr_budget_closes_only_the_ocr_lane_and_charges_nothing() -> None:
    demo, fakes = _demo(SCANNED, usage=UsageSnapshot(ocr_pages_today=299), limits=DemoLimits(daily_ocr_page_budget=300))
    seen, on_stage = _stages()

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", on_stage)

    assert isinstance(outcome, Refusal)
    assert outcome.kind == "daily-budget"
    assert outcome.reason == "the OCR budget for today is spent: 299 of 300 pages used and this PDF needs 2 more"
    assert outcome.plan is not None and outcome.plan.decision.needs_ocr is True
    assert "warming" not in seen and "reading" not in seen
    # A refusal spends nothing at all: not a page against the day, and not a read against the visitor's
    # window, so a budget that closes does not also eat one of their five reads.
    assert fakes.ledger.reads == [] and fakes.ledger.charged == 0
    assert fakes.ocr.calls == 0


def test_a_native_read_is_admitted_even_when_the_ocr_budget_is_spent() -> None:
    demo, fakes = _demo(HEALTHY, usage=UsageSnapshot(ocr_pages_today=300), limits=DemoLimits(daily_ocr_page_budget=300))

    outcome = demo.read(b"%PDF", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert fakes.ledger.reads == ["10.0.0.1"]
    assert fakes.ledger.charged == 0
    assert fakes.native.calls == 1


def test_an_admitted_ocr_read_records_one_read_and_charges_the_pages_it_will_ocr() -> None:
    demo, fakes = _demo(SCANNED)

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert fakes.ledger.reads == ["10.0.0.1"]
    assert fakes.ledger.charged == SCANNED.page_count
    assert fakes.ocr.calls == 1


def test_bytes_the_probe_cannot_measure_are_refused_in_the_probe_s_own_words() -> None:
    demo, fakes = _demo(
        HEALTHY, probe_failure=ValueError("this file is not a PDF: no %PDF- header in the first 1 KiB")
    )

    outcome = demo.read(b"definitely not a pdf", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Refusal)
    assert outcome.kind == "unreadable"
    assert outcome.reason == "this file is not a PDF: no %PDF- header in the first 1 KiB"
    assert outcome.plan is None
    assert fakes.ledger.reads == [] and fakes.ledger.charged == 0
    assert fakes.native.calls == 0 and fakes.ocr.calls == 0


def test_a_cold_endpoint_is_announced_as_a_warming_stage_with_its_measured_wait() -> None:
    demo, fakes = _demo(SCANNED, answers=[False, False, True])
    seen, on_stage = _stages()

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", on_stage)

    assert isinstance(outcome, Reading)
    assert seen == ["routing", "warming", "reading", "done"]
    assert outcome.warm_ms == 4000
    assert fakes.time.sleeps == [2.0, 2.0]
    assert fakes.endpoint.calls == 3
    assert outcome.engine == fakes.ocr.identity


def test_a_warm_endpoint_announces_no_warming_stage_and_no_wait_of_its_own() -> None:
    demo, fakes = _demo(SCANNED, answers=[True])
    seen, on_stage = _stages()

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", on_stage)

    assert isinstance(outcome, Reading)
    assert seen == ["routing", "reading", "done"]
    assert outcome.warm_ms == 0, "a worker that answered at once was waited for zero milliseconds"
    assert fakes.time.sleeps == []
    assert fakes.endpoint.calls == 1


def test_a_first_readiness_question_that_waited_is_measured_as_the_wait() -> None:
    """The first question can be what starts a container: its own duration is part of the number."""
    demo, fakes = _demo(SCANNED)
    fakes.worker.endpoint = _SlowFirstAnswer(fakes.time)

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert outcome.warm_ms == 8000, "a cold start answered on the first question is still a cold start"


def test_an_endpoint_that_never_answers_is_a_failed_reading_naming_the_timeout() -> None:
    demo, fakes = _demo(SCANNED, answers=[False], warm_timeout_seconds=5.0, poll_interval_seconds=2.0)
    seen, on_stage = _stages()

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", on_stage)

    assert isinstance(outcome, FailedReading)
    assert outcome.kind == "failed"
    assert outcome.lane == "ocr"
    assert outcome.reason == "the OCR worker did not answer within 5 s"
    assert outcome.plan is not None and outcome.plan.decision.needs_ocr is True
    assert seen == ["routing", "warming"]
    # The charge is made when the read is admitted, before anything is waited for: the GPU this read
    # asked for is the demo's cost whether or not the worker came up.
    assert fakes.ledger.charged == SCANNED.page_count
    assert fakes.ocr.calls == 0


def test_an_endpoint_that_cannot_be_asked_is_a_failed_reading_that_never_waits() -> None:
    demo, fakes = _demo(
        SCANNED,
        endpoint_failure=OcrEndpointUnavailable("the OCR endpoint has no base URL configured"),
    )
    seen, on_stage = _stages()

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", on_stage)

    assert isinstance(outcome, FailedReading)
    assert outcome.reason == "the OCR endpoint has no base URL configured"
    assert outcome.lane == "ocr"
    assert seen == ["routing"]
    assert fakes.time.sleeps == []
    assert fakes.ocr.calls == 0


def test_a_lane_failure_is_a_failed_reading_in_the_error_s_own_words() -> None:
    demo, fakes = _demo(HEALTHY, native_failure=RuntimeError("pdf is encrypted and cannot be opened"))
    seen, on_stage = _stages()

    outcome = demo.read(b"%PDF", "10.0.0.1", on_stage)

    assert isinstance(outcome, FailedReading)
    assert outcome.kind == "failed"
    assert outcome.lane == "text-layer"
    assert outcome.reason == "pdf is encrypted and cannot be opened"
    assert outcome.plan is not None and outcome.plan.decision.needs_ocr is False
    assert seen == ["routing", "reading"]
    assert fakes.ocr.calls == 0


def test_an_ocr_lane_failure_is_a_failed_reading_naming_the_ocr_lane() -> None:
    demo, fakes = _demo(SCANNED, ocr_failure=RuntimeError("the model answered HTTP 500"))

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, FailedReading)
    assert outcome.lane == "ocr"
    assert outcome.reason == "the model answered HTTP 500"
    assert fakes.native.calls == 0


def test_a_successful_read_writes_no_file(tmp_path: Path, monkeypatch: Any) -> None:
    demo, _ = _demo(SCANNED)
    monkeypatch.chdir(tmp_path)

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert list(tmp_path.rglob("*")) == []

class _WritingRunner:
    """A stand-in for the ``paddleocr`` CLI: one result per rendered page, written where the CLI writes.

    It writes only into the ``--save_path`` it is handed, and remembers which page images it could see,
    so a test can tell the engine's own scratch directory from a file the read left behind.
    """

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.images: list[Path] = []
        self.output: Path | None = None

    def run(self, argv: list[str], cwd: Path | None = None) -> CommandResult:
        self.argv = list(argv)
        images_dir = Path(argv[argv.index("-i") + 1])
        self.output = Path(argv[argv.index("--save_path") + 1])
        self.images = sorted(images_dir.glob("*.png"))
        self.output.mkdir(parents=True, exist_ok=True)  # the CLI creates its save directory, so this does too
        for number, image in enumerate(self.images, start=1):
            payload = {
                "res": {
                    "page_index": number - 1,
                    "parsing_res_list": [
                        {"block_content": f"read from page {number}", "block_label": "text", "block_order": 0}
                    ],
                }
            }
            (self.output / f"{image.stem}_res.json").write_text(json.dumps(payload), encoding="utf-8")
        return CommandResult(argv=tuple(argv), returncode=0, stdout="", stderr="")


def _image_only_pdf(pages: int = 1) -> bytes:
    """A PDF with no text layer at all, which is what routes a document to the OCR lane."""
    document = pymupdf.open()
    for _ in range(pages):
        page = document.new_page(width=595, height=842)
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), 0)
        page.insert_image(pymupdf.Rect(60, 60, 500, 700), pixmap=pixmap)
    content = document.tobytes()
    document.close()
    return content


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_an_ocr_read_writes_only_into_a_scratch_directory_it_removes(tmp_path: Path, monkeypatch: Any) -> None:
    """The retention promise on the lane that writes at all: page images exist during the read, then nothing.

    The CLI engine renders every page to a PNG so each page keeps its own result file, which is the one
    way a Reading touches a disk. So the images are a scratch directory's contents -- they must be there
    while the command runs and gone when the Reading answers, or "the demo keeps nothing" is false.
    """
    work = tmp_path / "work"
    spool = tmp_path / "tmp"
    work.mkdir()
    spool.mkdir()
    monkeypatch.setenv("TMPDIR", str(spool))
    monkeypatch.chdir(work)
    runner = _WritingRunner()
    engine = PaddleOcrVlCliEngine(command="paddleocr", work_dir=work, runner=runner)
    demo = DemoRead(
        probe=PymupdfTextLayerProbe(),
        engines={NATIVE_TEXT_ENGINE: FakeEngine(NATIVE_TEXT_ENGINE, [])},
        policy=CurationPolicy(engine_name=OCR_ENGINE, routing=OcrRoutingPolicy()),
        gates=SpendGatePolicy(DemoLimits()),
        ledger=FakeLedger(),
        worker=FakeWorker(engine, FakeEndpoint()),
        clock=FakeTime().clock,
        sleep=FakeTime().sleep,
    )
    before = {root: _tree(root) for root in (work, spool)}

    outcome = demo.read(_image_only_pdf(), "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert outcome.plan.lane == "ocr"
    assert [page.number for page in outcome.pages] == [1]
    assert [page.text for page in outcome.pages] == ["read from page 1"]
    # The command saw one rendered page image, and both the image and the result it wrote are gone.
    assert [image.name for image in runner.images] == ["page-0001.png"]
    assert not runner.images[0].exists()
    assert runner.output is not None and not runner.output.exists()
    assert {root: _tree(root) for root in (work, spool)} == before, "the demo kept a page image or a result"




def test_a_native_read_never_enters_a_worker() -> None:
    """Nothing is rented for a document this host can read: the worker is not even built."""
    demo, fakes = _demo(HEALTHY)

    outcome = demo.read(b"%PDF healthy", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert fakes.worker.entered == 0


def test_a_refused_upload_never_enters_a_worker() -> None:
    demo, fakes = _demo(SCANNED, limits=DemoLimits(page_cap=1))

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Refusal)
    assert outcome.kind == "page-cap"
    assert fakes.worker.entered == 0, "a refusal must not rent the GPU it never uses"


def test_an_admitted_ocr_read_enters_and_leaves_the_worker_exactly_once() -> None:
    demo, fakes = _demo(SCANNED)

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert (fakes.worker.entered, fakes.worker.exited) == (1, 1)
    assert fakes.ocr.calls == 1, "the engine the worker published is the one that read"


def test_a_worker_that_cannot_be_entered_is_an_unavailable_refusal_in_its_own_words() -> None:
    demo, fakes = _demo(SCANNED, worker_failure=RuntimeError("modal: the start command exited 1"))

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Refusal)
    assert outcome.kind == "unavailable"
    assert "modal: the start command exited 1" in outcome.reason
    assert fakes.ocr.calls == 0, "no lane ran, so no lane failed"
    assert fakes.worker.exited == 0, "a worker that was never entered is not given back"


def test_a_worker_that_never_starts_charges_neither_the_window_nor_the_budget() -> None:
    """The gates admitted it, but nothing was read: a start command that keeps failing must cost nothing."""
    demo, fakes = _demo(SCANNED, worker_failure=RuntimeError("modal: the start command exited 1"))

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Refusal)
    assert outcome.kind == "unavailable"
    assert fakes.ledger.reads == [], "a read that never started is not a read against the visitor"
    assert fakes.ledger.charged == 0, "no page reached the GPU, so no page is charged to the day"


def test_a_release_that_fails_after_a_reading_keeps_the_reading(caplog: Any) -> None:
    """The visitor's text is real; a stop command that failed is the operator's log line (ADR-0007)."""
    demo, fakes = _demo(SCANNED, release_failure=RuntimeError("the stop command timed out"))

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, Reading)
    assert fakes.worker.exited == 1
    assert "the stop command timed out" in caplog.text


def test_a_failure_reason_is_shown_through_the_redactor() -> None:
    """An engine's own error can quote the worker's address, and the job answer is public."""
    address = "https://workspace--finepdfs-demo-ocr.modal.run/v1"
    demo, _ = _demo(
        SCANNED,
        ocr_failure=RuntimeError(f"the model request to {address} failed: connection reset"),
        redact=lambda message: message.replace(address, "<the OCR worker>"),
    )

    outcome = demo.read(b"%PDF scanned", "10.0.0.1", lambda stage: None)

    assert isinstance(outcome, FailedReading)
    assert "modal.run" not in outcome.reason
    assert "<the OCR worker>" in outcome.reason
