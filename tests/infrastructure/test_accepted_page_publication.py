"""Accepted Page Texts through real CocoIndex runs and declared localfs outputs.

The PDF is a recorded Common Crawl payload; Engine text and Page Languages are controlled
fixtures, not evidence of OCR or language-identification quality.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import cocoindex as coco
import pytest

from finepdfs.application.curate import CurationPolicy, CurationStep, ExtractionStep
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText
from finepdfs.domain.language import LanguagePolicy, PageLanguage
from finepdfs.domain.publishing import PublishPolicy
from finepdfs.infrastructure.cocoindex import flow
from finepdfs.infrastructure.cocoindex.runtime import Runtime
from finepdfs.infrastructure.corpus.index import document_key
from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextLayerProbe

URL = "https://example.org/mixed.pdf"


class ControlledEngine:
    identity = EngineIdentity("pymupdf", "controlled-pages")

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, document: Document) -> ExtractedDocument:
        self.calls += 1
        return ExtractedDocument(
            pages=(
                PageText(1, "Rejected uncertain page with enough extra words to inflate the character count."),
                PageText(2, "  Accepted second page.  "),
                PageText(3, "Rejected foreign page that must not contribute to published text or length."),
                PageText(4, "Accepted fourth page."),
            ),
            engine=self.identity,
        )


class ControlledLanguages:
    def identify(self, text: str, page_number: int) -> PageLanguage:
        language, confidence = {1: ("eng", 0.1), 2: ("eng", 0.9), 3: ("fra", 0.9), 4: ("eng", 0.9)}[page_number]
        return PageLanguage(page_number, language, confidence)


def _runtime(tmp_path: Path) -> Runtime:
    corpus_dir = tmp_path / "corpus"
    (corpus_dir / "pdfs").mkdir(parents=True)
    content = (Path(__file__).resolve().parents[1] / "fixtures" / "warc_payload.bin").read_bytes()
    document = Document(URL, content)
    pdf_file = relative_pdf_path(document.digest)
    (corpus_dir / pdf_file).write_bytes(content)
    entry = CorpusEntry(
        url=URL, digest=document.digest, file=pdf_file, bytes=len(content),
        crawl_id="controlled", warc_filename="controlled", offset=0, length=len(content),
    )
    CorpusManifest(crawl_id="controlled", domain="example.org", entries=[entry]).save(corpus_dir / "manifest.json")
    policy = CurationPolicy(engine_name="pymupdf", publish=PublishPolicy(min_characters=40))
    return Runtime(
        corpus_dir=corpus_dir,
        out_dir=tmp_path / "out",
        policy=policy,
        extraction=ExtractionStep(PymupdfTextLayerProbe(), {"pymupdf": ControlledEngine()}, policy),
        curation=CurationStep(ControlledLanguages(), policy),
    )


def _run(monkeypatch, tmp_path: Path, runtime: Runtime) -> None:
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / "flow.db"))
    flow.configure_runtime(runtime)

    async def update() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    asyncio.run(update())


def test_only_accepted_original_pages_contribute_to_published_text_and_counts(monkeypatch, tmp_path) -> None:
    runtime = _runtime(tmp_path)
    _run(monkeypatch, tmp_path, runtime)

    key = document_key(URL)
    markdown = (runtime.out_dir / "docs" / f"{key}.md").read_text()
    assert markdown == "Accepted second page.\n\nAccepted fourth page."
    record = json.loads((runtime.out_dir / "records" / f"{key}.json").read_text())
    assert record["character_count"] == 42
    assert record["page_count"] == 4
    assert record["accepted_pages"] == 2
    assert record["dropped_pages"] == 2
    assert record["language"] == "eng"


@pytest.mark.parametrize(
    ("publish", "language", "reason"),
    [
        (PublishPolicy(min_characters=44), LanguagePolicy(), "43 characters is fewer than the 44 character minimum"),
        (PublishPolicy(min_pages=3, min_characters=0), LanguagePolicy(), "2 pages is fewer than the 3 page minimum"),
        (PublishPolicy(), LanguagePolicy(min_confidence=1.0), "0 pages is fewer than the 1 page minimum"),
        (PublishPolicy(min_pages=0, min_characters=1), LanguagePolicy(min_confidence=1.0), "0 characters is fewer than the 1 character minimum"),
    ],
    ids=["accepted-character-minimum", "accepted-page-minimum", "all-pages-rejected", "all-characters-rejected"],
)
def test_publish_policy_judges_only_accepted_content_on_a_warm_rerun(
    monkeypatch, tmp_path, publish, language, reason,
) -> None:
    runtime = _runtime(tmp_path)
    _run(monkeypatch, tmp_path, runtime)
    key = document_key(URL)
    markdown_path = runtime.out_dir / "docs" / f"{key}.md"
    record_path = runtime.out_dir / "records" / f"{key}.json"
    assert markdown_path.exists() and record_path.exists()

    policy = replace(runtime.policy, publish=publish, language=language)
    stricter = replace(runtime, policy=policy, curation=CurationStep(ControlledLanguages(), policy))
    _run(monkeypatch, tmp_path, stricter)

    exclusion = json.loads((runtime.out_dir / "excluded" / f"{key}.json").read_text())
    assert exclusion["url"] == URL
    assert exclusion["kind"] == "dropped"
    assert exclusion["reason"] == reason
    assert not markdown_path.exists()
    assert not record_path.exists()
    assert runtime.engines["pymupdf"].calls == 1, "cheap policy changes must reuse Extraction"
