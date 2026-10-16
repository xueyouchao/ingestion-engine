"""The CocoIndex flow, run end to end against a small corpus of real PDFs.

This is the acceptance seam for the incremental behaviour: re-running must reprocess nothing, and
changing a cheap rule must re-run only the cheap half.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import cocoindex as coco
import pytest

from finepdfs.application.curate import CurationPolicy, CurationStep, ExtractionStep
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.domain.publishing import PublishPolicy
from finepdfs.infrastructure.cocoindex import flow
from finepdfs.infrastructure.cocoindex.runtime import Runtime
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


def _runtime(corpus_dir: Path, out_dir: Path, *, policy: CurationPolicy | None = None, probe=None, engines=None) -> Runtime:
    policy = policy or CurationPolicy(engine_name="pymupdf")
    engines = engines or {"pymupdf": PymupdfTextEngine()}
    probe = probe or PymupdfTextLayerProbe()
    return Runtime(
        corpus_dir=corpus_dir,
        out_dir=out_dir,
        policy=policy,
        extraction=ExtractionStep(probe, engines, policy),
        curation=CurationStep(ScriptLanguageIdentifier(), policy),
    )


@pytest.fixture
def corpus(tmp_path) -> Path:
    corpus_dir = tmp_path / "corpus"
    _write_corpus(corpus_dir)
    return corpus_dir


def _run(monkeypatch, tmp_path: Path, runtime: Runtime, name: str) -> None:
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / f"{name}.db"))
    monkeypatch.setattr(flow, "runtime_from_environment", lambda *_: runtime)

    async def update() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    __import__("asyncio").run(update())


def _record(out_dir: Path, url: str) -> dict:
    return json.loads((out_dir / "records" / f"{document_key(url)}.json").read_text())


def _exclusion(out_dir: Path, url: str) -> dict:
    return json.loads((out_dir / "excluded" / f"{document_key(url)}.json").read_text())


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
