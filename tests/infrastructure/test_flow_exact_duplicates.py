"""Declared duplicate decisions through real CocoIndex runs; synthetic text, no real OCR."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import cocoindex as coco
import pytest

from finepdfs.application.curate import CurationPolicy, CurationStep, ExtractionStep
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, content_digest
from finepdfs.infrastructure.cocoindex import flow
from finepdfs.infrastructure.cocoindex.runtime import Runtime
from finepdfs.infrastructure.corpus.index import document_key
from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextLayerProbe
from finepdfs.infrastructure.language.script import ScriptLanguageIdentifier

KEEPER = "https://a.example/document.pdf"
SECOND = "https://b.example/document.pdf"
THIRD = "https://c.example/document.pdf"
NEAR = "https://d.example/document.pdf"


class CountingEngine:
    """Different PDF bytes yield identical controlled text: near, not exact, duplication."""

    identity = EngineIdentity(name="pymupdf", detail="duplicate-regression-v1")

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, document: Document) -> ExtractedDocument:
        self.calls += 1
        return ExtractedDocument(
            pages=(PageText(number=1, text="The committee reviewed meteorological instruments in Geneva. " * 20),),
            engine=self.identity,
        )


@pytest.fixture
def corpus_runtime(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    (corpus / "pdfs").mkdir(parents=True)
    # A real local PDF keeps routing realistic; Engine text remains a controlled fixture.
    body = (Path(__file__).resolve().parents[1] / "fixtures" / "warc_payload.bin").read_bytes()
    entries = []
    for url in (THIRD, NEAR, SECOND, KEEPER):  # deliberately not keeper order
        content = body + b"\n% distinct PDF bytes\n" if url == NEAR else body
        digest = content_digest(content)
        (corpus / relative_pdf_path(digest)).write_bytes(content)
        entries.append(CorpusEntry(
            url=url, digest=digest, file=relative_pdf_path(digest), bytes=len(content),
            crawl_id="controlled", warc_filename="controlled.warc.gz", offset=0, length=len(content),
        ))
    CorpusManifest(crawl_id="controlled", domain="example", entries=entries).save(corpus / "manifest.json")
    engine = CountingEngine()
    policy = CurationPolicy(engine_name="pymupdf")
    runtime = Runtime(
        corpus_dir=corpus, out_dir=tmp_path / "out", policy=policy,
        extraction=ExtractionStep(PymupdfTextLayerProbe(), {"pymupdf": engine}, policy),
        curation=CurationStep(ScriptLanguageIdentifier(), policy),
    )
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / "flow.db"))
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "none")
    return runtime, engine


def _run(runtime: Runtime) -> None:
    flow.configure_runtime(runtime)

    async def update() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    asyncio.run(update())


def _report(runtime: Runtime, name: str) -> list[dict]:
    return [json.loads(line) for line in (runtime.out_dir / "dedupe" / name).read_text().splitlines()]


def test_exact_and_near_reports_agree_with_exclusions_without_extra_extraction(corpus_runtime) -> None:
    runtime, engine = corpus_runtime
    _run(runtime)

    expected = [
        {"url": SECOND, "kind": "exact", "duplicate_of": KEEPER, "similarity": 1.0},
        {"url": THIRD, "kind": "exact", "duplicate_of": KEEPER, "similarity": 1.0},
        {"url": NEAR, "kind": "near", "duplicate_of": KEEPER, "similarity": 1.0},
    ]
    assert _report(runtime, "duplicates.jsonl") == expected
    assert _report(runtime, "kept.jsonl") == [{"url": KEEPER}]
    assert [record["url"] for record in runtime.published().read_records()] == [KEEPER]
    assert sorted(path.name for path in (runtime.out_dir / "docs").glob("*.md")) == [f"{document_key(KEEPER)}.md"]
    exclusions = {entry["url"]: entry for entry in runtime.published().read_exclusions()}
    assert set(exclusions) == {SECOND, THIRD, NEAR}
    for decision in expected:
        exclusion = exclusions[decision["url"]]
        assert exclusion["kind"] == f"{decision['kind']}-duplicate"
        assert decision["duplicate_of"] in exclusion["reason"]
    assert engine.calls == 2, "one Extraction per digest, not per URL"

    _run(runtime)
    assert _report(runtime, "duplicates.jsonl") == expected
    assert engine.calls == 2, "unchanged reruns reuse Extraction"


def test_removing_keepers_promotes_survivors_and_retracts_obsolete_decisions(corpus_runtime) -> None:
    runtime, engine = corpus_runtime
    _run(runtime)

    for removed, keeper, expected in (
        (KEEPER, SECOND, [
            {"url": THIRD, "kind": "exact", "duplicate_of": SECOND, "similarity": 1.0},
            {"url": NEAR, "kind": "near", "duplicate_of": SECOND, "similarity": 1.0},
        ]),
        (SECOND, THIRD, [{"url": NEAR, "kind": "near", "duplicate_of": THIRD, "similarity": 1.0}]),
        (THIRD, NEAR, []),
    ):
        manifest = CorpusManifest.load(runtime.corpus_dir / "manifest.json")
        manifest.entries = [entry for entry in manifest.entries if entry.url != removed]
        manifest.save(runtime.corpus_dir / "manifest.json")
        _run(runtime)

        assert _report(runtime, "duplicates.jsonl") == expected
        assert _report(runtime, "kept.jsonl") == [{"url": keeper}]
        assert [record["url"] for record in runtime.published().read_records()] == [keeper]
        assert sorted(path.name for path in (runtime.out_dir / "docs").glob("*.md")) == [f"{document_key(keeper)}.md"]
        exclusions = {entry["url"]: entry for entry in runtime.published().read_exclusions()}
        assert set(exclusions) == {decision["url"] for decision in expected}
        for decision in expected:
            assert exclusions[decision["url"]]["kind"] == f"{decision['kind']}-duplicate"
            assert keeper in exclusions[decision["url"]]["reason"]
        assert engine.calls == 2, "promoting an exact or near survivor must reuse its digest's Extraction"

        _run(runtime)
        assert _report(runtime, "duplicates.jsonl") == expected
        assert engine.calls == 2
