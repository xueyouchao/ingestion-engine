"""Declared page exports through real CocoIndex runs.

A published Document exports its accepted Page Texts with their original 1-based numbers, and the
declared target state retracts them when a later run no longer publishes that source. The PDF is a
recorded Common Crawl payload; Engine text and Page Languages are controlled fixtures, not evidence of
OCR or language-identification quality.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import cocoindex as coco

from finepdfs.application.curate import CurationPolicy, CurationStep, ExtractionStep
from finepdfs.domain.document import EngineIdentity, ExtractedDocument, PageText, content_digest
from finepdfs.domain.language import LanguagePolicy, PageLanguage
from finepdfs.domain.publishing import PublishPolicy
from finepdfs.infrastructure.cocoindex import flow
from finepdfs.infrastructure.cocoindex.runtime import Runtime
from finepdfs.infrastructure.corpus.index import document_key
from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextLayerProbe

URL = "https://example.org/mixed.pdf"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "warc_payload.bin"


class ControlledEngine:
    """Page texts keyed by PDF digest. A stand-in for an Engine, not a quality claim."""

    identity = EngineIdentity("pymupdf", "controlled-pages")

    def __init__(self, pages_by_digest: dict[str, tuple[PageText, ...]]) -> None:
        self._pages = pages_by_digest
        self.calls = 0

    def extract(self, document, sink=None) -> ExtractedDocument:
        self.calls += 1
        return ExtractedDocument(pages=self._pages[document.digest], engine=self.identity)


class ControlledLanguages:
    """A fixed language and confidence per page number; real identification is not under test."""

    def __init__(self, by_page: dict[int, tuple[str, float]] | None = None) -> None:
        self._by_page = by_page or {}

    def identify(self, text: str, page_number: int) -> PageLanguage:
        language, confidence = self._by_page.get(page_number, ("eng", 0.9))
        return PageLanguage(page_number, language, confidence)


def _write_corpus(corpus_dir: Path, bodies: dict[str, bytes]) -> None:
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / "pdfs").mkdir(exist_ok=True)
    entries = []
    for url, content in bodies.items():
        digest = content_digest(content)
        (corpus_dir / relative_pdf_path(digest)).write_bytes(content)
        entries.append(
            CorpusEntry(
                url=url, digest=digest, file=relative_pdf_path(digest), bytes=len(content),
                crawl_id="controlled", warc_filename="controlled.warc.gz", offset=0, length=len(content),
            )
        )
    CorpusManifest(crawl_id="controlled", domain="example.org", entries=entries).save(corpus_dir / "manifest.json")


def _runtime(
    tmp_path: Path,
    engine: ControlledEngine,
    languages: ControlledLanguages,
    *,
    policy: CurationPolicy | None = None,
) -> Runtime:
    policy = policy or CurationPolicy(engine_name="pymupdf", publish=PublishPolicy(min_characters=40))
    return Runtime(
        corpus_dir=tmp_path / "corpus",
        out_dir=tmp_path / "out",
        policy=policy,
        extraction=ExtractionStep(PymupdfTextLayerProbe(), {"pymupdf": engine}, policy),
        curation=CurationStep(languages, policy),
    )


def _run(monkeypatch, tmp_path: Path, runtime: Runtime, name: str = "runs") -> None:
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / f"{name}.db"))
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "none")
    flow.configure_runtime(runtime)

    async def update() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    asyncio.run(update())


def _pages_path(out_dir: Path, url: str) -> Path:
    return out_dir / "pages" / f"{document_key(url)}.jsonl"


def _page_records(out_dir: Path, url: str) -> list[dict]:
    return [json.loads(line) for line in _pages_path(out_dir, url).read_text().splitlines()]


MIXED_PAGES = (
    PageText(1, "Rejected uncertain page with enough extra words to inflate the character count."),
    PageText(2, "  Accepted second page.  "),
    PageText(3, "Rejected foreign page that must not contribute to published text or length."),
    PageText(4, "Accepted fourth page."),
)


def test_published_pages_export_original_numbers_identity_and_engine(monkeypatch, tmp_path) -> None:
    body = FIXTURE.read_bytes()
    _write_corpus(tmp_path / "corpus", {URL: body})
    engine = ControlledEngine({content_digest(body): MIXED_PAGES})
    languages = ControlledLanguages({1: ("eng", 0.1), 2: ("eng", 0.9), 3: ("fra", 0.9), 4: ("eng", 0.9)})
    runtime = _runtime(tmp_path, engine, languages)

    _run(monkeypatch, tmp_path, runtime)

    records = _page_records(runtime.out_dir, URL)
    assert [record["page_number"] for record in records] == [2, 4], "original 1-based numbers, not ordinals"
    assert [record["text"] for record in records] == ["Accepted second page.", "Accepted fourth page."]
    for record in records:
        assert record["doc_id"] == document_key(URL)
        assert record["source_url"] == URL
        assert record["digest"] == content_digest(body)
        assert record["engine"] == "pymupdf@controlled-pages"

    markdown = (runtime.out_dir / "docs" / f"{document_key(URL)}.md").read_text()
    assert markdown == "\n\n".join(record["text"] for record in records), "Markdown is the same projection"


KEEPER = "https://a.example/keeper.pdf"
EXACT = "https://b.example/exact.pdf"
NEAR = "https://c.example/near.pdf"
DROPPED = "https://d.example/dropped.pdf"

TWO_PAGES = (
    PageText(1, "Accepted page one carries enough words to clear the character minimum."),
    PageText(2, "Accepted page two carries enough words to clear the minimum as well."),
)
UNCERTAIN = PageText(3, "Rejected uncertain page that no reader may cite.")
PAGE_LANGUAGES = {1: ("eng", 0.9), 2: ("eng", 0.9), 3: ("eng", 0.1)}


def _exclusion(out_dir: Path, url: str) -> dict:
    return json.loads((out_dir / "excluded" / f"{document_key(url)}.json").read_text())


def test_duplicate_and_dropped_documents_export_no_pages(monkeypatch, tmp_path) -> None:
    keeper_body = FIXTURE.read_bytes()
    near_body = keeper_body + b"\n% distinct bytes carrying the same text\n"
    dropped_body = keeper_body + b"\n% a third document whose only page is uncertain\n"
    _write_corpus(
        tmp_path / "corpus",
        {KEEPER: keeper_body, EXACT: keeper_body, NEAR: near_body, DROPPED: dropped_body},
    )
    engine = ControlledEngine(
        {
            content_digest(keeper_body): TWO_PAGES,
            content_digest(near_body): TWO_PAGES,
            content_digest(dropped_body): (UNCERTAIN,),
        }
    )
    runtime = _runtime(tmp_path, engine, ControlledLanguages(PAGE_LANGUAGES))

    _run(monkeypatch, tmp_path, runtime)

    assert [record["page_number"] for record in _page_records(runtime.out_dir, KEEPER)] == [1, 2]
    for url in (EXACT, NEAR, DROPPED):
        assert not _pages_path(runtime.out_dir, url).exists(), f"{url} is not published"
        assert not (runtime.out_dir / "records" / f"{document_key(url)}.json").exists()
    assert _exclusion(runtime.out_dir, EXACT)["kind"] == "exact-duplicate"
    assert _exclusion(runtime.out_dir, NEAR)["kind"] == "near-duplicate"
    assert _exclusion(runtime.out_dir, DROPPED)["kind"] == "dropped"
    assert engine.calls == 3, "one Extraction per distinct digest"

    _run(monkeypatch, tmp_path, runtime)

    assert [record["page_number"] for record in _page_records(runtime.out_dir, KEEPER)] == [1, 2]
    for url in (EXACT, NEAR, DROPPED):
        assert not _pages_path(runtime.out_dir, url).exists(), "a rerun must not resurrect an export"
    assert engine.calls == 3, "an unchanged rerun reuses Extraction"


FIRST = (
    PageText(1, "First accepted page carries enough words to clear the minimum."),
    PageText(3, "Third accepted page carries enough words to clear the minimum."),
    PageText(5, "Fifth accepted page carries enough words to clear the minimum."),
)
REPLACEMENT = (PageText(2, "Only surviving page, still long enough to be published."),)


def test_replacing_content_with_fewer_pages_keeps_identity_and_retracts_obsolete_pages(
    monkeypatch, tmp_path
) -> None:
    first_body = FIXTURE.read_bytes()
    second_body = first_body + b"\n% replacement bytes for the same source\n"
    _write_corpus(tmp_path / "corpus", {URL: first_body})
    engine = ControlledEngine(
        {content_digest(first_body): FIRST, content_digest(second_body): REPLACEMENT}
    )
    runtime = _runtime(tmp_path, engine, ControlledLanguages())

    _run(monkeypatch, tmp_path, runtime)

    assert [record["page_number"] for record in _page_records(runtime.out_dir, URL)] == [1, 3, 5]
    assert _page_records(runtime.out_dir, URL)[0]["digest"] == content_digest(first_body)

    _write_corpus(tmp_path / "corpus", {URL: second_body})
    _run(monkeypatch, tmp_path, runtime)

    records = _page_records(runtime.out_dir, URL)
    assert [record["page_number"] for record in records] == [2], "the obsolete pages are gone"
    assert records[0]["text"] == REPLACEMENT[0].text
    assert records[0]["doc_id"] == document_key(URL), "the source identity survives the replacement"
    assert records[0]["digest"] == content_digest(second_body)
    assert sorted(path.name for path in (runtime.out_dir / "pages").glob("*.jsonl")) == [
        f"{document_key(URL)}.jsonl"
    ], "a replacement rewrites its own export instead of leaving a second one behind"
    markdown = (runtime.out_dir / "docs" / f"{document_key(URL)}.md").read_text()
    assert markdown == REPLACEMENT[0].text
    assert engine.calls == 2, "different bytes are a different Extraction, and only that one re-ran"


def _keep_only(corpus_dir: Path, urls: set[str]) -> None:
    manifest = CorpusManifest.load(corpus_dir / "manifest.json")
    manifest.entries = [entry for entry in manifest.entries if entry.url in urls]
    manifest.save(corpus_dir / "manifest.json")


SECOND_URL = "https://example.org/second.pdf"


def test_removing_a_crawl_record_removes_its_page_export(monkeypatch, tmp_path) -> None:
    first_body = FIXTURE.read_bytes()
    second_body = first_body + b"\n% a second, unrelated source\n"
    _write_corpus(tmp_path / "corpus", {URL: first_body, SECOND_URL: second_body})
    engine = ControlledEngine(
        {
            content_digest(first_body): TWO_PAGES,
            content_digest(second_body): (PageText(1, "Another published document, long enough to be kept."),),
        }
    )
    runtime = _runtime(tmp_path, engine, ControlledLanguages())

    _run(monkeypatch, tmp_path, runtime)
    assert _pages_path(runtime.out_dir, URL).exists()
    assert _pages_path(runtime.out_dir, SECOND_URL).exists()

    _keep_only(tmp_path / "corpus", {SECOND_URL})
    _run(monkeypatch, tmp_path, runtime)

    assert not _pages_path(runtime.out_dir, URL).exists(), "a removed Crawl Record retracts its export"
    assert not (runtime.out_dir / "docs" / f"{document_key(URL)}.md").exists()
    assert not (runtime.out_dir / "excluded" / f"{document_key(URL)}.json").exists(), (
        "a removed source is gone, not an exclusion"
    )
    assert _pages_path(runtime.out_dir, SECOND_URL).exists()


PROMOTED_KEEPER = "https://a.example/shared.pdf"
PROMOTED_SECOND = "https://b.example/shared.pdf"
PROMOTED_THIRD = "https://c.example/shared.pdf"


def test_removing_the_keeper_promotes_a_survivor_and_moves_its_page_export(monkeypatch, tmp_path) -> None:
    shared_body = FIXTURE.read_bytes()
    _write_corpus(
        tmp_path / "corpus",
        {PROMOTED_THIRD: shared_body, PROMOTED_SECOND: shared_body, PROMOTED_KEEPER: shared_body},
    )
    engine = ControlledEngine({content_digest(shared_body): TWO_PAGES})
    runtime = _runtime(tmp_path, engine, ControlledLanguages())

    _run(monkeypatch, tmp_path, runtime)

    assert _pages_path(runtime.out_dir, PROMOTED_KEEPER).exists()
    for url in (PROMOTED_SECOND, PROMOTED_THIRD):
        assert not _pages_path(runtime.out_dir, url).exists(), f"{url} is an exact duplicate"
    assert engine.calls == 1

    _keep_only(tmp_path / "corpus", {PROMOTED_SECOND, PROMOTED_THIRD})
    _run(monkeypatch, tmp_path, runtime)

    records = _page_records(runtime.out_dir, PROMOTED_SECOND)
    assert [record["page_number"] for record in records] == [1, 2]
    assert records[0]["doc_id"] == document_key(PROMOTED_SECOND)
    assert records[0]["source_url"] == PROMOTED_SECOND
    assert records[0]["digest"] == content_digest(shared_body)
    assert not _pages_path(runtime.out_dir, PROMOTED_KEEPER).exists(), (
        "the removed keeper's export is retracted, not left beside the promoted survivor's"
    )
    assert not _pages_path(runtime.out_dir, PROMOTED_THIRD).exists(), "the third stays an exact duplicate"
    assert engine.calls == 1, "a promoted keeper reuses its digest's Extraction"

    _run(monkeypatch, tmp_path, runtime)
    assert _page_records(runtime.out_dir, PROMOTED_SECOND) == records, "a rerun keeps the same export"
    assert engine.calls == 1


def test_a_language_policy_change_retracts_the_pages_it_stops_accepting(monkeypatch, tmp_path) -> None:
    body = FIXTURE.read_bytes()
    _write_corpus(tmp_path / "corpus", {URL: body})
    engine = ControlledEngine({content_digest(body): TWO_PAGES})
    languages = ControlledLanguages({1: ("eng", 0.95), 2: ("eng", 0.9)})
    runtime = _runtime(tmp_path, engine, languages)

    _run(monkeypatch, tmp_path, runtime)
    assert [record["page_number"] for record in _page_records(runtime.out_dir, URL)] == [1, 2]

    narrower = replace(runtime.policy, language=LanguagePolicy(min_confidence=0.92))
    stricter = replace(runtime, policy=narrower, curation=CurationStep(languages, narrower))
    _run(monkeypatch, tmp_path, stricter)

    assert [record["page_number"] for record in _page_records(runtime.out_dir, URL)] == [1], (
        "the page the policy no longer accepts must leave the export"
    )
    markdown = (runtime.out_dir / "docs" / f"{document_key(URL)}.md").read_text()
    assert markdown == TWO_PAGES[0].text

    rejected = replace(runtime.policy, language=LanguagePolicy(min_confidence=0.96))
    nothing = replace(runtime, policy=rejected, curation=CurationStep(languages, rejected))
    _run(monkeypatch, tmp_path, nothing)

    assert not _pages_path(runtime.out_dir, URL).exists(), "a dropped document exports no pages"
    assert not (runtime.out_dir / "docs" / f"{document_key(URL)}.md").exists()
    assert _exclusion(runtime.out_dir, URL)["kind"] == "dropped"
    assert engine.calls == 1, "policy changes are outside the memoised unit"
