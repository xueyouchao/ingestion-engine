"""The CocoIndex flow, run end to end against a small corpus of real PDFs.

This is the acceptance seam for the incremental behaviour: re-running must reprocess nothing, and
changing a cheap rule must re-run only the cheap half.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import cocoindex as coco
import pytest

from finepdfs.application.curate import CurationPolicy, CurationStep, ExtractionStep
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
from finepdfs.domain.language import PageLanguage
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.domain.publishing import PublishPolicy
from finepdfs.infrastructure.cocoindex import flow
from finepdfs.infrastructure.cocoindex.runtime import Runtime
from finepdfs.infrastructure.compute.providers import GpuStopFailed
from finepdfs.infrastructure.corpus.index import document_key
from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextEngine, PymupdfTextLayerProbe
from finepdfs.infrastructure.language.script import ScriptLanguageIdentifier

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

ONE = "https://a.example/one.pdf"
DUPLICATE = "https://b.example/one.pdf"
BROKEN = "https://c.example/broken.pdf"

SCANNED = TextLayerQuality(page_count=1, pages_with_text=0, character_count=0, garbled_ratio=0.0, image_dominated_pages=1)


class CountingEngine:
    """A stand-in engine that counts how often it was actually called."""

    def __init__(self, name: str, pages: tuple[PageText, ...], detail: str = "counting") -> None:
        self._identity = EngineIdentity(name=name, detail=detail)
        self._pages = pages
        self.calls = 0

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document) -> ExtractedDocument:
        self.calls += 1
        return ExtractedDocument(pages=self._pages, engine=self._identity)


class ScanningProbe:
    """Reports every document as a scan, so routing picks the configured engine."""

    def measure(self, content: bytes) -> TextLayerQuality:
        return SCANNED


def _write_corpus(corpus_dir: Path) -> None:
    real_pdf = (FIXTURES / "warc_payload.bin").read_bytes()
    broken = b"this file is not a pdf at all"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / "pdfs").mkdir(exist_ok=True)

    entries = []
    for url, content in ((ONE, real_pdf), (DUPLICATE, real_pdf), (BROKEN, broken)):
        digest = __import__("hashlib").sha256(content).hexdigest()
        (corpus_dir / relative_pdf_path(digest)).write_bytes(content)
        entries.append(
            CorpusEntry(
                url=url,
                digest=digest,
                file=relative_pdf_path(digest),
                bytes=len(content),
                crawl_id="CC-MAIN-2026-39",
                warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
                offset=1,
                length=len(content),
            )
        )
    CorpusManifest(crawl_id="CC-MAIN-2026-39", domain="a.example", entries=entries).save(corpus_dir / "manifest.json")


def _runtime(corpus_dir: Path, out_dir: Path, *, policy: CurationPolicy | None = None, probe=None, engines=None, languages=None) -> Runtime:
    policy = policy or CurationPolicy(engine_name="pymupdf")
    engines = engines or {"pymupdf": PymupdfTextEngine()}
    probe = probe or PymupdfTextLayerProbe()
    return Runtime(
        corpus_dir=corpus_dir,
        out_dir=out_dir,
        policy=policy,
        extraction=ExtractionStep(probe, engines, policy),
        curation=CurationStep(languages or ScriptLanguageIdentifier(), policy),
    )


@pytest.fixture
def corpus(tmp_path) -> Path:
    corpus_dir = tmp_path / "corpus"
    _write_corpus(corpus_dir)
    return corpus_dir


def _run(monkeypatch, tmp_path: Path, runtime: Runtime, name: str) -> None:
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / f"{name}.db"))
    flow.configure_runtime(runtime)

    async def update() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    __import__("asyncio").run(update())


def _record(out_dir: Path, url: str) -> dict:
    return json.loads((out_dir / "records" / f"{document_key(url)}.json").read_text())


def _exclusion(out_dir: Path, url: str) -> dict:
    return json.loads((out_dir / "excluded" / f"{document_key(url)}.json").read_text())


def test_a_failed_run_gives_the_worker_back_and_clears_the_endpoint(monkeypatch, tmp_path) -> None:
    """ADR-0007: the lease wraps the run, so its effects must not outlive it either.

    This drives the flow's own lifespan, not the provider: a run that fails after the lease was taken
    must still hand the worker back, and must not leave the process naming a worker it has returned.
    """
    started = tmp_path / "worker.started"
    stopped = tmp_path / "worker.stopped"
    start_script = tmp_path / "start_worker.py"
    start_script.write_text(
        "import json\n"
        f"open({str(started)!r}, 'w').write('up')\n"
        "print(json.dumps({'base_url': 'http://127.0.0.1:9/v1', 'model': 'm', 'identity': 'flow:test'}))\n"
    )
    stop_script = tmp_path / "stop_worker.py"
    stop_script.write_text(f"open({str(stopped)!r}, 'w').write('down')\n")

    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "manifest.json").write_text("{ this manifest is not JSON")

    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.setenv("FINEPDFS_GPU_START_COMMAND", f"{sys.executable} {start_script}")
    monkeypatch.setenv("FINEPDFS_GPU_STOP_COMMAND", f"{sys.executable} {stop_script}")
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / "flow.db"))
    monkeypatch.setenv("FINEPDFS_CORPUS", str(corpus_dir))
    monkeypatch.setenv("FINEPDFS_ENGINE", "paddleocr-vl")
    monkeypatch.setenv("FINEPDFS_ENGINE_MODEL", "operator-pinned")
    monkeypatch.delenv("FINEPDFS_ENGINE_BASE_URL", raising=False)
    monkeypatch.delenv("FINEPDFS_ENGINE_API_KEY", raising=False)
    monkeypatch.delenv("FINEPDFS_ENGINE_IDENTITY", raising=False)
    flow.configure_runtime(None)

    async def failing_run() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    with pytest.raises(ValueError):
        asyncio.run(failing_run())

    assert started.exists(), "the flow's lifespan must have leased a worker"
    assert stopped.exists(), "a leaked GPU bills by the hour"
    assert "FINEPDFS_ENGINE_BASE_URL" not in os.environ, "the returned worker must not stay the engine"
    assert "FINEPDFS_ENGINE_API_KEY" not in os.environ
    assert "FINEPDFS_ENGINE_IDENTITY" not in os.environ
    assert os.environ["FINEPDFS_ENGINE_MODEL"] == "operator-pinned", "the lease's export must be undone"
    with pytest.raises(RuntimeError):
        flow.active_runtime()


def test_a_stop_command_that_fails_after_a_good_run_reaches_the_flow(monkeypatch, tmp_path, corpus) -> None:
    """The release runs at the flow's own lifespan, so a leak must reach the caller from there too.

    The provider tests drive the lease directly; this drives the seam production actually takes --
    `cocoindex update main.py` -- where the lease is taken inside the lifespan. The run publishes, then
    the stop command fails: the work is done, the worker is not back, and that is a provider failure
    rather than something CocoIndex's own teardown may swallow.
    """
    start_script = tmp_path / "start_worker.py"
    start_script.write_text(
        "import json\n"
        "print(json.dumps({'base_url': 'http://127.0.0.1:9/v1', 'model': 'm', 'identity': 'flow:test'}))\n"
    )
    stop_script = tmp_path / "stop_worker.py"
    stop_script.write_text("import sys\nsys.exit(3)\n")

    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.setenv("FINEPDFS_GPU_START_COMMAND", f"{sys.executable} {start_script}")
    monkeypatch.setenv("FINEPDFS_GPU_STOP_COMMAND", f"{sys.executable} {stop_script}")
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / "flow.db"))
    monkeypatch.setenv("FINEPDFS_CORPUS", str(corpus))
    monkeypatch.setenv("FINEPDFS_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("FINEPDFS_ENGINE", "pymupdf")
    monkeypatch.delenv("FINEPDFS_ENGINE_BASE_URL", raising=False)
    monkeypatch.delenv("FINEPDFS_ENGINE_API_KEY", raising=False)
    monkeypatch.delenv("FINEPDFS_ENGINE_IDENTITY", raising=False)
    flow.configure_runtime(None)

    async def good_run() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    with pytest.raises(GpuStopFailed, match="exited with 3"):
        asyncio.run(good_run())

    assert (tmp_path / "out" / "records" / f"{document_key(ONE)}.json").exists(), "the run did its work"
    assert "FINEPDFS_ENGINE_BASE_URL" not in os.environ, "the returned worker must not stay the engine"
    assert "FINEPDFS_ENGINE_API_KEY" not in os.environ


def test_a_missing_stop_binary_after_a_good_run_reaches_the_flow(monkeypatch, tmp_path, corpus) -> None:
    """A stop binary that is not installed fails before there is any exit code to read.

    ``SubprocessRunner`` raises ``CommandFailed`` itself for a binary ``shutil.which`` cannot find,
    which is not the same shape as ``require_success`` reading a nonzero exit. At the flow's own
    lifespan that still has to be the provider's failure -- ``GpuStopFailed``, which the CLI reports
    -- rather than a bare ``CommandFailed`` escaping as the run ends.
    """
    start_script = tmp_path / "start_worker.py"
    start_script.write_text(
        "import json\n"
        "print(json.dumps({'base_url': 'http://127.0.0.1:9/v1', 'model': 'm', 'identity': 'flow:test'}))\n"
    )

    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.setenv("FINEPDFS_GPU_START_COMMAND", f"{sys.executable} {start_script}")
    monkeypatch.setenv("FINEPDFS_GPU_STOP_COMMAND", "/no/such/stop-worker")
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / "flow.db"))
    monkeypatch.setenv("FINEPDFS_CORPUS", str(corpus))
    monkeypatch.setenv("FINEPDFS_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("FINEPDFS_ENGINE", "pymupdf")
    monkeypatch.delenv("FINEPDFS_ENGINE_BASE_URL", raising=False)
    monkeypatch.delenv("FINEPDFS_ENGINE_API_KEY", raising=False)
    monkeypatch.delenv("FINEPDFS_ENGINE_IDENTITY", raising=False)
    flow.configure_runtime(None)

    async def good_run() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    with pytest.raises(GpuStopFailed, match="not installed"):
        asyncio.run(good_run())

    assert (tmp_path / "out" / "records" / f"{document_key(ONE)}.json").exists(), "the run did its work"


def test_a_missing_stop_binary_does_not_mask_the_error_that_ended_the_run(monkeypatch, tmp_path) -> None:
    """CocoIndex resumes the lifespan generator, so the run's error is never thrown into the lease.

    The provider tests deliver the run's failure to ``lease`` by raising inside its ``with``; under
    ``cocoindex update`` that never happens, because the environment closes the generator with
    ``next()``. The stop leak must still be reported beside the error that ended the run rather than
    in its place, because that error is the one the operator has to act on (ADR-0007).
    """
    start_script = tmp_path / "start_worker.py"
    start_script.write_text(
        "import json\n"
        "print(json.dumps({'base_url': 'http://127.0.0.1:9/v1', 'model': 'm', 'identity': 'flow:test'}))\n"
    )
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "manifest.json").write_text("{ this manifest is not JSON")

    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "command")
    monkeypatch.setenv("FINEPDFS_GPU_START_COMMAND", f"{sys.executable} {start_script}")
    monkeypatch.setenv("FINEPDFS_GPU_STOP_COMMAND", "/no/such/stop-worker")
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / "flow.db"))
    monkeypatch.setenv("FINEPDFS_CORPUS", str(corpus_dir))
    monkeypatch.setenv("FINEPDFS_ENGINE", "paddleocr-vl")
    monkeypatch.setenv("FINEPDFS_ENGINE_MODEL", "operator-pinned")
    monkeypatch.delenv("FINEPDFS_ENGINE_BASE_URL", raising=False)
    monkeypatch.delenv("FINEPDFS_ENGINE_API_KEY", raising=False)
    monkeypatch.delenv("FINEPDFS_ENGINE_IDENTITY", raising=False)
    flow.configure_runtime(None)

    async def failing_run() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    with pytest.raises(ValueError) as raised:
        asyncio.run(failing_run())

    notes = " ".join(getattr(raised.value, "__notes__", []))
    assert "GPU stop command" in notes, "the leak is reported on the run's traceback, not in its place"
    assert "not installed" in notes, "the note says what the stop attempt actually did"


def test_a_run_publishes_real_text_records_and_exclusions(monkeypatch, tmp_path, corpus) -> None:
    out_dir = tmp_path / "out"
    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir), "first")

    markdown = (out_dir / "docs" / f"{document_key(ONE)}.md").read_text()
    assert len(markdown) > 500
    record = _record(out_dir, ONE)
    assert record["url"] == ONE
    assert record["engine"].startswith("pymupdf@")
    assert record["routing_decision"] == "text-layer"
    assert record["language"] == "Latn"
    assert record["page_count"] >= 1 and record["character_count"] > 200

    duplicate = _exclusion(out_dir, DUPLICATE)
    assert duplicate["kind"] == "exact-duplicate"
    assert ONE in duplicate["reason"]

    broken = _exclusion(out_dir, BROKEN)
    assert broken["kind"] == "failed"
    assert "not a readable PDF" in broken["reason"] or "PdfReadError" in broken["reason"]


def test_a_second_run_reprocesses_nothing(monkeypatch, tmp_path, corpus) -> None:
    out_dir = tmp_path / "out"
    engine = CountingEngine("pymupdf", (), detail="counting")
    runtime = _runtime(corpus, out_dir, engines={"pymupdf": engine})

    _run(monkeypatch, tmp_path, runtime, "runs")
    assert engine.calls == 1

    _run(monkeypatch, tmp_path, runtime, "runs")

    assert engine.calls == 1, "the second run should have been served from the memo"


def test_changing_a_cheap_rule_does_not_re_extract(monkeypatch, tmp_path, corpus) -> None:
    out_dir = tmp_path / "out"
    engine = CountingEngine("pymupdf", (PageText(number=1, text="word " * 300),), detail="counting")
    runtime = _runtime(corpus, out_dir, engines={"pymupdf": engine})
    _run(monkeypatch, tmp_path, runtime, "runs")
    assert engine.calls == 1

    strict = _runtime(
        corpus,
        out_dir,
        policy=CurationPolicy(engine_name="pymupdf", publish=PublishPolicy(min_characters=10_000_000)),
        engines={"pymupdf": engine},
    )
    _run(monkeypatch, tmp_path, strict, "runs")

    assert engine.calls == 1, "publish rules are outside the memoised unit"
    exclusion = _exclusion(out_dir, ONE)
    assert "character minimum" in exclusion["reason"]


class CountingIdentifier:
    """A stand-in language identifier that counts the pages it was asked to name."""

    def __init__(self, language: str) -> None:
        self._language = language
        self.calls = 0

    def identify(self, text: str, page_number: int) -> PageLanguage:
        self.calls += 1
        return PageLanguage(page_number=page_number, language=self._language, confidence=1.0)


def test_switching_the_language_adapter_rewrites_the_records(monkeypatch, tmp_path, corpus) -> None:
    """A mode switch re-curates, because language identification is outside the memoised unit.

    ADR-0001 keys the memo on the engine identity so switching engines re-extracts. The language
    mode is a second, mode-dependent identity, but it changes no extraction: it changes the recorded
    language. So the mode belongs in no memo key, and what an operator needs to know is that the next
    run re-runs the deciding pass and rewrites the record -- not that the mode silently fails to take.
    """
    out_dir = tmp_path / "out"
    script = CountingIdentifier("Latn")
    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, languages=script), "runs")
    assert _record(out_dir, ONE)["language"] == "Latn"

    language = CountingIdentifier("eng")
    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, languages=language), "runs")

    assert language.calls > 0, "the deciding pass runs every run, whatever the extraction memo holds"
    assert _record(out_dir, ONE)["language"] == "eng", "the record carries the adapter that is wired now"


def _keep_only(corpus_dir: Path, urls: set[str]) -> None:
    """Leave a corpus holding just these documents."""
    manifest = CorpusManifest.load(corpus_dir / "manifest.json")
    manifest.entries = [entry for entry in manifest.entries if entry.url in urls]
    manifest.save(corpus_dir / "manifest.json")


def test_naming_another_engine_re_extracts_with_it(monkeypatch, tmp_path, corpus) -> None:
    _keep_only(corpus, {ONE})
    out_dir = tmp_path / "out"
    text_engine = CountingEngine("pymupdf", (), detail="counting")
    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, engines={"pymupdf": text_engine}), "runs")
    assert text_engine.calls == 1

    ocr = CountingEngine("paddleocr-vl", (PageText(number=1, text="ocr " * 300),), detail="counting")
    rescanned = _runtime(
        corpus,
        out_dir,
        policy=CurationPolicy(engine_name="paddleocr-vl", routing=OcrRoutingPolicy(min_characters_per_page=1e9)),
        probe=ScanningProbe(),
        engines={"pymupdf": text_engine, "paddleocr-vl": ocr},
    )
    _run(monkeypatch, tmp_path, rescanned, "runs")

    assert ocr.calls == 1
    assert text_engine.calls == 1
    record = _record(out_dir, ONE)
    assert record["engine"] == "paddleocr-vl@counting"
    assert record["routing_decision"] == "ocr"
    assert "ocr ocr" in (out_dir / "docs" / f"{document_key(ONE)}.md").read_text()


def test_a_run_removes_outputs_for_documents_it_no_longer_has(monkeypatch, tmp_path) -> None:
    corpus_dir = tmp_path / "corpus"
    _write_corpus(corpus_dir)
    out_dir = tmp_path / "out"
    _run(monkeypatch, tmp_path, _runtime(corpus_dir, out_dir), "runs")
    assert (out_dir / "docs" / f"{document_key(ONE)}.md").exists()

    manifest = CorpusManifest.load(corpus_dir / "manifest.json")
    manifest.entries = [entry for entry in manifest.entries if entry.url != ONE]
    manifest.save(corpus_dir / "manifest.json")
    _run(monkeypatch, tmp_path, _runtime(corpus_dir, out_dir), "runs")

    assert not (out_dir / "docs" / f"{document_key(ONE)}.md").exists()
    assert not (out_dir / "records" / f"{document_key(ONE)}.json").exists()


class NamedEngine:
    """A stand-in engine whose text depends on which document it is reading."""

    def __init__(self, pages_by_length: dict[int, tuple[str, ...]], detail: str = "named") -> None:
        self._identity = EngineIdentity(name="paddleocr-vl", detail=detail)
        self._pages = pages_by_length
        self.calls = 0

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document) -> ExtractedDocument:
        self.calls += 1
        texts = self._pages[len(document.content)]
        pages = tuple(PageText(number=index + 1, text=text) for index, text in enumerate(texts))
        return ExtractedDocument(pages=pages, engine=self._identity)


def _page_text(page: int, *, annex: str = "") -> str:
    """A page of distinct Latin lines, with something extra on the last page of a near-duplicate."""
    lines = [
        f"Page {page}, paragraph {index}: the committee reviewed meteorological instrument {index} "
        f"in geneva on day {index} of the session."
        for index in range(1, 13)
    ]
    return "\n".join(lines) + annex


@dataclass(frozen=True, slots=True)
class _Entry:
    url: str
    body: bytes


def _write_near_duplicate_corpus(corpus_dir: Path, entries: tuple[_Entry, ...]) -> None:
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / "pdfs").mkdir(exist_ok=True)
    manifest_entries = []
    for entry in entries:
        digest = hashlib.sha256(entry.body).hexdigest()
        (corpus_dir / relative_pdf_path(digest)).write_bytes(entry.body)
        manifest_entries.append(
            CorpusEntry(
                url=entry.url,
                digest=digest,
                file=relative_pdf_path(digest),
                bytes=len(entry.body),
                crawl_id="CC-MAIN-2026-39",
                warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
                offset=1,
                length=len(entry.body),
            )
        )
    CorpusManifest(crawl_id="CC-MAIN-2026-39", domain="a.example", entries=manifest_entries).save(
        corpus_dir / "manifest.json"
    )


NEAR = "https://b.example/near.pdf"


def test_a_near_duplicate_is_excluded_and_stays_excluded(monkeypatch, tmp_path) -> None:
    corpus_dir = tmp_path / "corpus"
    body_a = b"%PDF-1.5 first document body"
    body_b = b"%PDF-1.5 second document body, different bytes"
    _write_near_duplicate_corpus(
        corpus_dir, (_Entry(url=ONE, body=body_a), _Entry(url=NEAR, body=body_b))
    )
    pages = tuple(_page_text(page) for page in range(1, 4))
    engine = NamedEngine(
        {
            len(body_a): pages,
            len(body_b): (*pages[:2], pages[2] + "\nAnnex: the instruments went back to the depot."),
        }
    )
    runtime = Runtime(
        corpus_dir=corpus_dir,
        out_dir=tmp_path / "out",
        policy=CurationPolicy(engine_name="paddleocr-vl"),
        extraction=ExtractionStep(ScanningProbe(), {"paddleocr-vl": engine}, CurationPolicy(engine_name="paddleocr-vl")),
        curation=CurationStep(ScriptLanguageIdentifier(), CurationPolicy(engine_name="paddleocr-vl")),
    )

    _run(monkeypatch, tmp_path, runtime, "runs")

    out_dir = tmp_path / "out"
    assert (out_dir / "docs" / f"{document_key(ONE)}.md").exists()
    assert not (out_dir / "docs" / f"{document_key(NEAR)}.md").exists()
    assert not (out_dir / "records" / f"{document_key(NEAR)}.json").exists()
    exclusion = _exclusion(out_dir, NEAR)
    assert exclusion["kind"] == "near-duplicate"
    assert ONE in exclusion["reason"]

    kept = (out_dir / "dedupe" / "kept.jsonl").read_text()
    duplicates = (out_dir / "dedupe" / "duplicates.jsonl").read_text()
    assert ONE in kept and NEAR not in kept
    assert NEAR in duplicates and '"kind": "near"' in duplicates

    _run(monkeypatch, tmp_path, runtime, "runs")

    assert engine.calls == 2, "one call per document, and none on the second run"
    assert not (out_dir / "docs" / f"{document_key(NEAR)}.md").exists(), "a re-run must not resurrect it"
    assert _exclusion(out_dir, NEAR)["kind"] == "near-duplicate"


def test_a_corpus_file_that_no_longer_matches_its_digest_fails(monkeypatch, tmp_path) -> None:
    """Bytes swapped under a digest-named file must be a failure, not the old extraction again."""
    corpus_dir = tmp_path / "corpus"
    _write_corpus(corpus_dir)
    out_dir = tmp_path / "out"
    _run(monkeypatch, tmp_path, _runtime(corpus_dir, out_dir), "first")
    assert _record(out_dir, ONE)["url"] == ONE

    manifest = CorpusManifest.load(corpus_dir / "manifest.json")
    digest = next(entry.digest for entry in manifest.entries if entry.url == ONE)
    (corpus_dir / relative_pdf_path(digest)).write_bytes(b"%PDF-1.5 overwritten with other bytes")
    _run(monkeypatch, tmp_path, _runtime(corpus_dir, out_dir), "first")

    exclusion = _exclusion(out_dir, ONE)
    assert exclusion["kind"] == "failed"
    assert "digest" in exclusion["reason"]
    assert not (out_dir / "docs" / f"{document_key(ONE)}.md").exists()


def test_moving_the_corpus_does_not_send_anything_through_the_engine_again(monkeypatch, tmp_path) -> None:
    """The expensive memo is keyed on the content, not on where the corpus happens to live (ADR-0003)."""
    first_dir = tmp_path / "corpus-one"
    _write_corpus(first_dir)
    out_dir = tmp_path / "out"
    engine = CountingEngine("paddleocr-vl", (PageText(number=1, text="page text from the engine"),))
    policy = CurationPolicy(engine_name="paddleocr-vl")
    runtime = _runtime(first_dir, out_dir, policy=policy, probe=ScanningProbe(), engines={"paddleocr-vl": engine})
    _run(monkeypatch, tmp_path, runtime, "runs")
    calls = engine.calls
    assert calls > 0

    moved_dir = tmp_path / "corpus-two"
    shutil.copytree(first_dir, moved_dir)
    moved = _runtime(moved_dir, out_dir, policy=policy, probe=ScanningProbe(), engines={"paddleocr-vl": engine})
    _run(monkeypatch, tmp_path, moved, "runs")

    assert engine.calls == calls


def test_changing_the_engine_identity_re_extracts_even_though_the_name_is_the_same(monkeypatch, tmp_path) -> None:
    """A different --model changes the identity, and the memo key has to see it or the switch is ignored."""
    corpus_dir = tmp_path / "corpus"
    _write_corpus(corpus_dir)
    out_dir = tmp_path / "out"
    policy = CurationPolicy(engine_name="paddleocr-vl")

    first = CountingEngine("paddleocr-vl", (PageText(number=1, text="page text"),), detail="model-a")
    _run(
        monkeypatch,
        tmp_path,
        _runtime(corpus_dir, out_dir, policy=policy, probe=ScanningProbe(), engines={"paddleocr-vl": first}),
        "runs",
    )
    second = CountingEngine("paddleocr-vl", (PageText(number=1, text="page text"),), detail="model-b")
    _run(
        monkeypatch,
        tmp_path,
        _runtime(corpus_dir, out_dir, policy=policy, probe=ScanningProbe(), engines={"paddleocr-vl": second}),
        "runs",
    )

    assert first.calls > 0
    assert second.calls > 0, "the model switch must not be served from the previous memo entry"
