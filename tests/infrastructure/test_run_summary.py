"""The per-run summary: what a run actually did, counted at the seam where it happened.

The extract body is memoised, so an unchanged rerun must report zero calls and every requested
Extraction as reused. These tests use counting engines: they prove the accounting, not OCR.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import cocoindex as coco
import pytest

from finepdfs.application.curate import CurationPolicy, CurationStep, ExtractionStep
from finepdfs.domain.document import (
    Document,
    EngineIdentity,
    ExtractedDocument,
    PageText,
    TextLayerQuality,
)
from finepdfs.domain.publishing import PublishPolicy
from finepdfs.infrastructure.cocoindex import flow
from finepdfs.infrastructure.cocoindex.runtime import Runtime
from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextLayerProbe
from finepdfs.infrastructure.language.script import ScriptLanguageIdentifier

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

NATIVE = "https://a.example/native.pdf"
DUPLICATE = "https://b.example/native.pdf"
BROKEN = "https://c.example/broken.pdf"
SCAN = "https://d.example/scan.pdf"

SCANNED = TextLayerQuality(
    page_count=1, pages_with_text=0, character_count=0, garbled_ratio=0.0, image_dominated_pages=1
)
SCAN_BYTES = b"%PDF-SCAN this document only pretends to be bytes; the engine is a counter"


class CountingEngine:
    """A stand-in engine that counts how often it was actually called."""

    def __init__(self, name: str, pages: tuple[PageText, ...], detail: str = "counting") -> None:
        self._identity = EngineIdentity(name=name, detail=detail)
        self._pages = pages
        self.calls = 0

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document, sink=None) -> ExtractedDocument:
        self.calls += 1
        return ExtractedDocument(pages=self._pages, engine=self._identity)


class SelectiveProbe:
    """Real text-layer measurement, except for the one document this test calls a scan."""

    def __init__(self) -> None:
        self._native = PymupdfTextLayerProbe()

    def measure(self, content: bytes) -> TextLayerQuality:
        if content.startswith(b"%PDF-SCAN"):
            return SCANNED
        return self._native.measure(content)


def _entry(url: str, body: bytes) -> CorpusEntry:
    digest = hashlib.sha256(body).hexdigest()
    return CorpusEntry(
        url=url,
        digest=digest,
        file=relative_pdf_path(digest),
        bytes=len(body),
        crawl_id="CC-MAIN-2026-39",
        warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
        offset=1,
        length=len(body),
    )


def _write_corpus(corpus_dir: Path) -> None:
    real_pdf = (FIXTURES / "warc_payload.bin").read_bytes()
    broken = (FIXTURES / "not_a_pdf.pdf").read_bytes()
    entries = [_entry(url, body) for url, body in (
        (NATIVE, real_pdf),
        (DUPLICATE, real_pdf),
        (BROKEN, broken),
        (SCAN, SCAN_BYTES),
    )]
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / "pdfs").mkdir(exist_ok=True)
    for entry in entries:
        (corpus_dir / entry.file).write_bytes(
            real_pdf if entry.url in (NATIVE, DUPLICATE) else (broken if entry.url == BROKEN else SCAN_BYTES)
        )
    CorpusManifest(crawl_id="CC-MAIN-2026-39", domain="a.example", entries=entries).save(
        corpus_dir / "manifest.json"
    )


@dataclass(frozen=True, slots=True)
class _Engines:
    native: CountingEngine
    ocr: CountingEngine


def _engines() -> _Engines:
    # Distinct text, so the two published Documents are not near-duplicates of each other.
    native_page = PageText(number=1, text="word " * 300)
    ocr_page = PageText(number=1, text="scan text " * 300)
    return _Engines(
        native=CountingEngine("pymupdf", (native_page,), detail="test"),
        ocr=CountingEngine("docling", (ocr_page,), detail="cli:docling:v2.140.0"),
    )


def _runtime(corpus_dir: Path, out_dir: Path, engines: _Engines, *, policy: CurationPolicy | None = None) -> Runtime:
    policy = policy or CurationPolicy(engine_name="docling")
    return Runtime(
        corpus_dir=corpus_dir,
        out_dir=out_dir,
        policy=policy,
        extraction=ExtractionStep(
            SelectiveProbe(), {"pymupdf": engines.native, "docling": engines.ocr}, policy
        ),
        curation=CurationStep(ScriptLanguageIdentifier(), policy),
    )


def _run(monkeypatch, tmp_path: Path, runtime: Runtime, name: str) -> None:
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / f"{name}.db"))
    flow.configure_runtime(runtime)

    async def update() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    asyncio.run(update())


@pytest.fixture
def corpus(tmp_path) -> Path:
    corpus_dir = tmp_path / "corpus"
    _write_corpus(corpus_dir)
    return corpus_dir


def _summary(out_dir: Path) -> dict:
    return json.loads((out_dir / "run-summary.json").read_text())


def test_a_run_summary_counts_documents_routing_calls_publications_and_exclusions(
    monkeypatch, tmp_path, corpus
) -> None:
    out_dir = tmp_path / "out"
    engines = _engines()

    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, engines), "runs")

    summary = _summary(out_dir)
    assert summary["schema_version"] == 1
    assert summary["corpus"]["crawl_records"] == 4
    assert summary["corpus"]["documents"] == 3, "the exact-duplicate pair is one Document"
    assert summary["routing"] == {"documents": 2, "native": 1, "ocr": 1}, "the duplicate is not routed"
    assert summary["extraction"] == {"requested": 2, "calls": 2, "reused": 0}
    assert summary["published"] == {"documents": 2, "page_records": 2}
    assert summary["exclusions"] == {
        "total": 2,
        "by_kind": {"exact-duplicate": 1, "failed": 1, "dropped": 0, "near-duplicate": 0},
    }
    assert summary["engine"] == {"name": "docling", "identity": "docling@cli:docling:v2.140.0"}
    assert engines.native.calls == 1 and engines.ocr.calls == 1


def test_two_identical_runs_declare_a_byte_identical_summary(monkeypatch, tmp_path, corpus) -> None:
    """A published record must be reproducible: the same corpus, policy and warm state rewrite the same bytes.

    A cold run is a different run by definition -- it made Extraction calls the warm one reuses -- so
    "identical" means two runs over the same state, which is what ADR-0006's "run again: no changes" is.
    """
    out_dir = tmp_path / "out"
    engines = _engines()

    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, engines), "runs")
    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, engines), "runs")
    first = (out_dir / "run-summary.json").read_bytes()
    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, engines), "runs")

    assert (out_dir / "run-summary.json").read_bytes() == first
    assert not (out_dir / "last-run.json").exists(), (
        "the flow declares no timing log: observation belongs to the command wrapper, not target state"
    )


def test_the_declared_summary_carries_no_observation_of_when_the_run_happened(
    monkeypatch, tmp_path, corpus
) -> None:
    out_dir = tmp_path / "out"
    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, _engines()), "runs")

    summary = _summary(out_dir)

    assert set(summary) == {
        "schema_version",
        "corpus",
        "routing",
        "extraction",
        "published",
        "exclusions",
        "engine",
    }, "timing, host paths and lease data stay out of declared target state"
    assert "started_at" not in json.dumps(summary)
    assert "elapsed_seconds" not in json.dumps(summary)


def test_an_unchanged_rerun_makes_zero_actual_extractions_and_reports_the_reuse(
    monkeypatch, tmp_path, corpus
) -> None:
    out_dir = tmp_path / "out"
    engines = _engines()

    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, engines), "runs")
    assert engines.native.calls == 1 and engines.ocr.calls == 1

    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, engines), "runs")

    summary = _summary(out_dir)
    assert summary["extraction"] == {"requested": 2, "calls": 0, "reused": 2}, (
        "the memoised body did not run, so every requested key was served from CocoIndex's memo"
    )
    assert engines.native.calls == 1 and engines.ocr.calls == 1, "no engine was actually called again"
    assert summary["published"] == {"documents": 2, "page_records": 2}
    assert summary["routing"] == {"documents": 2, "native": 1, "ocr": 1}


def test_a_near_duplicate_is_counted_as_an_exclusion_not_a_publication(monkeypatch, tmp_path) -> None:
    """Every exclusion kind the flow declares is counted where it is declared, including this one."""
    corpus_dir = tmp_path / "corpus"
    urls = ("https://a.example/first.pdf", "https://b.example/second.pdf")
    bodies = (
        (FIXTURES / "warc_payload.bin").read_bytes(),
        (FIXTURES / "mixed_language.pdf").read_bytes(),
    )
    entries = [_entry(url, body) for url, body in zip(urls, bodies, strict=True)]
    (corpus_dir / "pdfs").mkdir(parents=True)
    for entry, body in zip(entries, bodies, strict=True):
        (corpus_dir / entry.file).write_bytes(body)
    CorpusManifest(crawl_id="CC-MAIN-2026-39", domain="a.example", entries=entries).save(
        corpus_dir / "manifest.json"
    )

    # The same text from two different Documents, so the deciding pass sees one near-duplicate pair.
    same_page = PageText(number=1, text="word " * 300)
    engine = CountingEngine("pymupdf", (same_page,), detail="test")
    policy = CurationPolicy(engine_name="pymupdf")
    runtime = Runtime(
        corpus_dir=corpus_dir,
        out_dir=tmp_path / "out",
        policy=policy,
        extraction=ExtractionStep(SelectiveProbe(), {"pymupdf": engine}, policy),
        curation=CurationStep(ScriptLanguageIdentifier(), policy),
    )

    _run(monkeypatch, tmp_path, runtime, "runs")

    summary = _summary(tmp_path / "out")
    assert summary["extraction"] == {"requested": 2, "calls": 2, "reused": 0}
    assert summary["published"] == {"documents": 1, "page_records": 1}
    assert summary["exclusions"] == {
        "total": 1,
        "by_kind": {"dropped": 0, "exact-duplicate": 0, "failed": 0, "near-duplicate": 1},
    }


def test_a_cheap_policy_change_reuses_extraction_and_updates_the_declared_output(
    monkeypatch, tmp_path, corpus
) -> None:
    from finepdfs.infrastructure.corpus.index import document_key

    out_dir = tmp_path / "out"
    engines = _engines()
    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, engines), "runs")
    markdown_path = out_dir / "docs" / f"{document_key(NATIVE)}.md"
    assert markdown_path.exists()

    strict = CurationPolicy(engine_name="docling", publish=PublishPolicy(min_characters=1_000_000))
    _run(monkeypatch, tmp_path, _runtime(corpus, out_dir, engines, policy=strict), "runs")

    summary = _summary(out_dir)
    assert summary["extraction"] == {"requested": 2, "calls": 0, "reused": 2}
    assert summary["published"] == {"documents": 0, "page_records": 0}
    assert summary["exclusions"]["by_kind"]["dropped"] == 3, (
        "the exact duplicate inherits the dropped disposition of its unpublished digest keeper"
    )
    assert summary["exclusions"]["by_kind"]["exact-duplicate"] == 0
    assert not markdown_path.exists(), "the published Markdown the new policy rejects is retracted"
    exclusion = json.loads((out_dir / "excluded" / f"{document_key(NATIVE)}.json").read_text())
    assert exclusion["kind"] == "dropped"
    assert "character minimum" in exclusion["reason"]
    assert engines.native.calls == 1 and engines.ocr.calls == 1


def test_a_second_update_on_the_same_runtime_reports_only_that_update(
    monkeypatch, tmp_path, corpus
) -> None:
    """`configure_runtime` reuses one runtime across updates, so a run's counters cannot be cumulative.

    A warm rerun is a run in its own right: it makes no engine call, reuses every Extraction it asks
    for, and publishes the same Documents once. Carrying the first run's counters forward would
    report both the cold run's calls as this run's and double every publication and exclusion.
    """
    out_dir = tmp_path / "out"
    runtime = _runtime(corpus, out_dir, _engines())

    _run(monkeypatch, tmp_path, runtime, "runs")
    assert _summary(out_dir)["extraction"] == {"requested": 2, "calls": 2, "reused": 0}

    _run(monkeypatch, tmp_path, runtime, "runs")

    summary = _summary(out_dir)
    assert summary["routing"] == {"documents": 2, "native": 1, "ocr": 1}, "routing is not doubled"
    assert summary["extraction"] == {"requested": 2, "calls": 0, "reused": 2}, (
        "the warm run reports its own reuse, not the cold run's calls"
    )
    assert summary["published"] == {"documents": 2, "page_records": 2}, "publications are not doubled"
    assert summary["exclusions"] == {
        "total": 2,
        "by_kind": {"dropped": 0, "exact-duplicate": 1, "failed": 1, "near-duplicate": 0},
    }, "exclusions are not doubled"


def test_an_unavailable_ocr_engine_does_not_fail_the_run_or_lose_native_documents(
    monkeypatch, tmp_path, corpus
) -> None:
    """The summary's identity probe runs outside the per-document handler; it must not end the run.

    A configured Docling whose executable is missing fails its version probe, and ADR-0005 keeps that
    version in the Engine Identity. Documents routed to it are excluded one by one; the native
    Documents already read still publish, and the summary names the unavailable engine
    deterministically instead of crashing or recording a host path.
    """
    from finepdfs.infrastructure.engines.docling_engine import DoclingEngine
    from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextEngine

    class MissingRunner:
        def run(self, arguments):
            raise FileNotFoundError(f"no executable named {arguments[0]}")

    out_dir = tmp_path / "out"
    policy = CurationPolicy(engine_name="docling")
    runtime = Runtime(
        corpus_dir=corpus,
        out_dir=out_dir,
        policy=policy,
        extraction=ExtractionStep(
            SelectiveProbe(),
            {
                "pymupdf": PymupdfTextEngine(),
                "docling": DoclingEngine(
                    command="/nonexistent/docling-for-summary", runner=MissingRunner()
                ),
            },
            policy,
        ),
        curation=CurationStep(ScriptLanguageIdentifier(), policy),
    )

    _run(monkeypatch, tmp_path, runtime, "runs")

    summary = _summary(out_dir)
    assert summary["engine"] == {"name": "docling", "identity": "unavailable"}, (
        "an engine that cannot name itself is reported deterministically, not as a host path"
    )
    assert summary["routing"] == {"documents": 2, "native": 1, "ocr": 1}
    assert summary["published"]["documents"] == 1, "a native Document already read is not lost"
    assert summary["published"]["page_records"] >= 1
    assert summary["exclusions"]["by_kind"]["failed"] == 2, (
        "the broken file and the Document routed to the unavailable engine are exclusions, not a run failure"
    )
    assert summary["exclusions"]["by_kind"]["exact-duplicate"] == 1
