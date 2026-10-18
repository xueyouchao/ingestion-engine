"""Declared chunk exports and the lexical search contract over them.

The search seam is the *declared* chunk files the flow writes, never a private table: a run declares
chunks for the accepted Page Texts of a published Document, and the local full-text index is a reader
of that target state. The PDF is a recorded Common Crawl payload and the page texts are controlled
fixtures, not evidence of OCR quality.

The Elasticsearch deployment the source spec proposed was cut for budget; SQLite FTS5 is the single
shipped backend behind the same record contract (docs/adr/0008-one-local-search-backend.md).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import cocoindex as coco
import pytest

from finepdfs.application.curate import CurationPolicy, CurationStep, ExtractionStep
from finepdfs.domain.chunks import CHUNKING_VERSION
from finepdfs.domain.document import EngineIdentity, ExtractedDocument, PageText, content_digest
from finepdfs.domain.language import PageLanguage
from finepdfs.domain.publishing import PageRecord, PublishPolicy
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

    def extract(self, document) -> ExtractedDocument:
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
                url=url,
                digest=digest,
                file=relative_pdf_path(digest),
                bytes=len(content),
                crawl_id="controlled",
                warc_filename="controlled.warc.gz",
                offset=0,
                length=len(content),
            )
        )
    CorpusManifest(crawl_id="controlled", domain="example.org", entries=entries).save(corpus_dir / "manifest.json")


def _keep_only(corpus_dir: Path, urls: set[str]) -> None:
    manifest = CorpusManifest.load(corpus_dir / "manifest.json")
    manifest.entries = [entry for entry in manifest.entries if entry.url in urls]
    manifest.save(corpus_dir / "manifest.json")


def _runtime(
    tmp_path: Path,
    engine: ControlledEngine,
    languages: ControlledLanguages,
    *,
    policy: CurationPolicy | None = None,
    search: bool = True,
) -> Runtime:
    policy = policy or CurationPolicy(engine_name="pymupdf", publish=PublishPolicy(min_characters=40))
    return Runtime(
        corpus_dir=tmp_path / "corpus",
        out_dir=tmp_path / "out",
        policy=policy,
        extraction=ExtractionStep(PymupdfTextLayerProbe(), {"pymupdf": engine}, policy),
        curation=CurationStep(languages, policy),
        search_enabled=search,
    )


def _run(monkeypatch, tmp_path: Path, runtime: Runtime, name: str = "runs") -> None:
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / f"{name}.db"))
    monkeypatch.setenv("FINEPDFS_GPU_PROVIDER", "none")
    flow.configure_runtime(runtime)

    async def update() -> None:
        async with coco.runtime():
            await flow.build_app().update()

    asyncio.run(update())


def _chunks_path(out_dir: Path, url: str) -> Path:
    return out_dir / "chunks" / f"{document_key(url)}.jsonl"


def _chunk_records(out_dir: Path, url: str) -> list[dict]:
    return [json.loads(line) for line in _chunks_path(out_dir, url).read_text().splitlines()]


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


def test_a_published_document_declares_chunks_of_its_accepted_pages(monkeypatch, tmp_path) -> None:
    body = FIXTURE.read_bytes()
    _write_corpus(tmp_path / "corpus", {URL: body})
    engine = ControlledEngine({content_digest(body): MIXED_PAGES})
    languages = ControlledLanguages({1: ("eng", 0.1), 2: ("eng", 0.9), 3: ("fra", 0.9), 4: ("eng", 0.9)})
    runtime = _runtime(tmp_path, engine, languages)

    _run(monkeypatch, tmp_path, runtime)

    chunks = _chunk_records(runtime.out_dir, URL)
    pages = _page_records(runtime.out_dir, URL)
    assert [chunk["page_number"] for chunk in chunks] == [2, 4], "accepted pages only, original numbers"
    assert [chunk["ordinal"] for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk["doc_id"] == document_key(URL)
        assert chunk["source_url"] == URL
        assert chunk["digest"] == content_digest(body)
        assert chunk["engine"] == "pymupdf@controlled-pages"
        assert chunk["language"] == "eng"
        assert chunk["chunking_version"] == CHUNKING_VERSION
        page_text = next(page["text"] for page in pages if page["page_number"] == chunk["page_number"])
        assert page_text[chunk["start_offset"] : chunk["end_offset"]] == chunk["text"]
    assert {page["page_number"] for page in pages} == {2, 4}
    assert set(pages[0]) == {"doc_id", "source_url", "digest", "page_number", "text", "engine"}, (
        "chunk exports do not change the page record contract"
    )


def _reindex(out_dir: Path, index_path: Path):
    from finepdfs.application.search import reindex_search
    from finepdfs.infrastructure.publishing.disk import PublishedCorpus
    from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex

    return reindex_search(SqliteFtsSearchIndex(index_path), PublishedCorpus(out_dir).read_declared_chunks())


def _search(out_dir: Path, index_path: Path, text: str, top_k: int = 20, **filters):
    """Read through the shared contract against the declared chunks in `out_dir`."""
    from finepdfs.application.search import SearchAcceptedEvidence
    from finepdfs.domain.search import SearchFilters
    from finepdfs.infrastructure.publishing.disk import PublishedCorpus
    from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex

    return SearchAcceptedEvidence(
        SqliteFtsSearchIndex(index_path), PublishedCorpus(out_dir).read_declared_chunks
    ).search(text, filters=SearchFilters(**filters), top_k=top_k)


def test_the_shared_search_api_returns_accepted_evidence_with_citations(monkeypatch, tmp_path) -> None:
    body = FIXTURE.read_bytes()
    _write_corpus(tmp_path / "corpus", {URL: body})
    pages = (
        PageText(1, "Alpine meteorology over the northern range."),
        PageText(2, "Rejected uncertain page that no reader may cite."),
        PageText(3, "Brighton buoy reports swell and wind at dawn."),
    )
    engine = ControlledEngine({content_digest(body): pages})
    languages = ControlledLanguages({1: ("eng", 0.9), 2: ("eng", 0.1), 3: ("eng", 0.9)})
    runtime = _runtime(tmp_path, engine, languages)
    _run(monkeypatch, tmp_path, runtime)

    index_path = tmp_path / "index.db"
    report = _reindex(runtime.out_dir, index_path)
    assert report.ok, report.failures
    assert report.chunks > 0

    results = _search(runtime.out_dir, index_path, "Brighton")
    assert len(results.hits) == 1
    hit = results.hits[0]
    assert hit.chunk.doc_id == document_key(URL)
    assert hit.chunk.chunk_id in {chunk["chunk_id"] for chunk in _chunk_records(runtime.out_dir, URL)}
    assert hit.chunk.source_url == URL
    assert hit.chunk.digest == content_digest(body)
    assert hit.chunk.page_number == 3
    assert hit.chunk.text.strip() == "Brighton buoy reports swell and wind at dawn."
    assert "<mark>Brighton</mark>" in hit.snippet
    assert hit.score > 0
    assert hit.chunk.engine == "pymupdf@controlled-pages"
    assert hit.chunk.language == "eng"

    assert _search(runtime.out_dir, index_path, "uncertain").hits == (), "only accepted pages are evidence"


def test_metadata_filters_narrow_the_same_contract(tmp_path) -> None:
    from finepdfs.domain.chunks import chunk_page_records

    first = PageRecord(
        doc_id="a" * 16,
        source_url="https://a.example/one.pdf",
        digest="1" * 64,
        page_number=1,
        text="Common harbour words",
        engine="pymupdf@native",
    )
    second = replace(
        first,
        doc_id="b" * 16,
        source_url="https://b.example/two.pdf",
        digest="2" * 64,
        engine="docling@cli",
    )
    index_path = tmp_path / "index.db"
    report = _reindex_chunks(
        index_path,
        chunk_page_records((first,), language="eng") + chunk_page_records((second,), language="fra"),
    )
    assert report.ok, report.failures
    chunks = chunk_page_records((first,), language="eng") + chunk_page_records((second,), language="fra")

    assert len(_search_chunks(index_path, chunks, "harbour").hits) == 2
    assert [hit.chunk.doc_id for hit in _search_chunks(index_path, chunks, "harbour", language="fra").hits] == ["b" * 16]
    assert [hit.chunk.doc_id for hit in _search_chunks(index_path, chunks, "harbour", engine="pymupdf@native").hits] == ["a" * 16]
    assert [hit.chunk.doc_id for hit in _search_chunks(index_path, chunks, "harbour", doc_id="a" * 16).hits] == ["a" * 16]
    assert _search_chunks(index_path, chunks, "harbour", language="eng", engine="docling@cli").hits == ()
    assert len(_search_chunks(index_path, chunks, "harbour", top_k=1).hits) == 1


def _reindex_chunks(index_path: Path, chunks):
    from finepdfs.application.search import reindex_search
    from finepdfs.domain.chunks import DeclaredChunkSet
    from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex

    return reindex_search(SqliteFtsSearchIndex(index_path), DeclaredChunkSet.of(chunks))


def good_chunks_hits(index_path: Path, chunk) -> tuple:
    """The hits for one stored chunk, read against a declared set that holds exactly it."""
    return _search_chunks(index_path, (chunk,), chunk.text.split()[0]).hits


def _search_chunks(index_path: Path, chunks, text: str, top_k: int = 20, **filters):
    """Read an index built from an in-memory declared set, without a published output directory."""
    from finepdfs.application.search import SearchAcceptedEvidence
    from finepdfs.domain.chunks import DeclaredChunkSet
    from finepdfs.domain.search import SearchFilters
    from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex

    declared = DeclaredChunkSet.of(chunks)
    return SearchAcceptedEvidence(SqliteFtsSearchIndex(index_path), lambda: declared).search(
        text, filters=SearchFilters(**filters), top_k=top_k
    )


def test_reindexing_twice_is_idempotent_and_never_duplicates_hits(monkeypatch, tmp_path) -> None:
    body = FIXTURE.read_bytes()
    _write_corpus(tmp_path / "corpus", {URL: body})
    engine = ControlledEngine({content_digest(body): (PageText(1, "Harbour dredging continues across the tidal range."),)})
    runtime = _runtime(tmp_path, engine, ControlledLanguages())
    _run(monkeypatch, tmp_path, runtime)
    index_path = tmp_path / "index.db"

    first = _reindex(runtime.out_dir, index_path)
    results = _search(runtime.out_dir, index_path, "harbour")
    retried = _reindex(runtime.out_dir, index_path)

    assert first.ok and retried.ok
    assert retried.removed == 0, "a retry declares exactly what is already stored"
    assert retried.upserted == retried.chunks
    assert _search(runtime.out_dir, index_path, "harbour").hits == results.hits
    assert len(results.hits) == 1
    assert len({hit.chunk.chunk_id for hit in results.hits}) == len(results.hits), "one row per chunk, no duplicates"


FIRST_PAGES = (
    PageText(1, "Alpine paragraph. " + "filler " * 300),
    PageText(3, "Bravo paragraph about buoys and wind."),
    PageText(5, "Charlie paragraph about harbour dredging."),
)
REPLACEMENT_PAGES = (PageText(2, "Delta paragraph, the only surviving accepted page."),)


def test_a_replacement_with_fewer_pages_retracts_old_chunks_and_their_hits(monkeypatch, tmp_path) -> None:
    first_body = FIXTURE.read_bytes()
    second_body = first_body + b"\n% replacement bytes for the same source\n"
    _write_corpus(tmp_path / "corpus", {URL: first_body})
    engine = ControlledEngine(
        {
            content_digest(first_body): FIRST_PAGES,
            content_digest(second_body): REPLACEMENT_PAGES,
        }
    )
    runtime = _runtime(tmp_path, engine, ControlledLanguages())
    _run(monkeypatch, tmp_path, runtime)
    index_path = tmp_path / "index.db"
    _reindex(runtime.out_dir, index_path)

    old_chunks = _chunk_records(runtime.out_dir, URL)
    old_ids = {chunk["chunk_id"] for chunk in old_chunks}
    filler_hits = _search(runtime.out_dir, index_path, "filler")
    assert len(filler_hits.hits) >= 2, "the long first page was split into several chunks"
    assert len({hit.chunk.chunk_id for hit in filler_hits.hits}) == len(filler_hits.hits)

    _write_corpus(tmp_path / "corpus", {URL: second_body})
    _run(monkeypatch, tmp_path, runtime)
    report = _reindex(runtime.out_dir, index_path)

    assert report.removed == len(old_ids), "every chunk of the replaced content is retracted"
    assert len(_chunk_records(runtime.out_dir, URL)) < len(old_chunks), "the replacement has fewer chunks"
    assert _search(runtime.out_dir, index_path, "Bravo").hits == ()
    assert _search(runtime.out_dir, index_path, "Charlie").hits == ()
    assert _search(runtime.out_dir, index_path, "filler").hits == ()
    delta = _search(runtime.out_dir, index_path, "Delta")
    assert len(delta.hits) == 1, "the surviving page is found exactly once"
    hit = delta.hits[0]
    assert hit.chunk.digest == content_digest(second_body)
    assert hit.chunk.page_number == 2
    assert hit.chunk.chunk_id not in old_ids
    assert [chunk["page_number"] for chunk in _chunk_records(runtime.out_dir, URL)] == [2]


def test_a_chunking_version_change_replaces_the_hits_instead_of_adding_them(monkeypatch, tmp_path) -> None:
    body = FIXTURE.read_bytes()
    _write_corpus(tmp_path / "corpus", {URL: body})
    engine = ControlledEngine(
        {content_digest(body): (PageText(1, "Alpine meteorology over the northern range."),)}
    )
    runtime = _runtime(tmp_path, engine, ControlledLanguages())
    _run(monkeypatch, tmp_path, runtime)
    index_path = tmp_path / "index.db"
    _reindex(runtime.out_dir, index_path)

    before = _search(runtime.out_dir, index_path, "meteorology")
    assert len(before.hits) == 1
    old_id = before.hits[0].chunk.chunk_id
    assert before.hits[0].chunk.chunking_version == CHUNKING_VERSION

    monkeypatch.setattr("finepdfs.domain.chunks.CHUNKING_VERSION", "chunking-v2")
    _run(monkeypatch, tmp_path, runtime)
    report = _reindex(runtime.out_dir, index_path)

    after = _search(runtime.out_dir, index_path, "meteorology")
    assert len(after.hits) == 1, "the same evidence is not returned twice under two chunking versions"
    assert after.hits[0].chunk.chunk_id != old_id
    assert after.hits[0].chunk.chunking_version == "chunking-v2"
    assert report.removed == 1, "the version-one row is retracted, not left beside the new one"
    assert after.hits[0].chunk.page_number == 1


def test_an_exclusion_stops_its_chunks_being_searchable(monkeypatch, tmp_path) -> None:
    body = FIXTURE.read_bytes()
    _write_corpus(tmp_path / "corpus", {URL: body})
    engine = ControlledEngine(
        {content_digest(body): (PageText(1, "Alpine meteorology over the northern range."),)}
    )
    runtime = _runtime(tmp_path, engine, ControlledLanguages())
    _run(monkeypatch, tmp_path, runtime)
    index_path = tmp_path / "index.db"
    _reindex(runtime.out_dir, index_path)
    assert len(_search(runtime.out_dir, index_path, "meteorology").hits) == 1

    dropped = _runtime(
        tmp_path,
        engine,
        ControlledLanguages(),
        policy=CurationPolicy(engine_name="pymupdf", publish=PublishPolicy(min_characters=10_000_000)),
    )
    _run(monkeypatch, tmp_path, dropped)
    report = _reindex(tmp_path / "out", index_path)

    assert not _chunks_path(tmp_path / "out", URL).exists(), "a dropped Document declares no chunks"
    assert report.removed == 1
    assert _search(runtime.out_dir, index_path, "meteorology").hits == ()


def test_removing_a_crawl_record_stops_its_chunks_being_searchable(monkeypatch, tmp_path) -> None:
    second_url = "https://example.org/second.pdf"
    first_body = FIXTURE.read_bytes()
    second_body = first_body + b"\n% a second, unrelated source\n"
    _write_corpus(tmp_path / "corpus", {URL: first_body, second_url: second_body})
    engine = ControlledEngine(
        {
            content_digest(first_body): (PageText(1, "Alpine meteorology over the northern range."),),
            content_digest(second_body): (PageText(1, "Brighton buoy reports swell and wind at dawn."),),
        }
    )
    runtime = _runtime(tmp_path, engine, ControlledLanguages())
    _run(monkeypatch, tmp_path, runtime)
    index_path = tmp_path / "index.db"
    _reindex(runtime.out_dir, index_path)
    assert len(_search(runtime.out_dir, index_path, "meteorology").hits) == 1

    _keep_only(tmp_path / "corpus", {second_url})
    _run(monkeypatch, tmp_path, runtime)
    report = _reindex(runtime.out_dir, index_path)

    assert report.removed == 1
    assert not _chunks_path(runtime.out_dir, URL).exists(), "a removed Crawl Record declares no chunks"
    assert _search(runtime.out_dir, index_path, "meteorology").hits == (), "a removed Crawl Record cannot supply evidence"
    assert len(_search(runtime.out_dir, index_path, "Brighton").hits) == 1, "the surviving document is still searchable"


def test_a_chunk_that_cannot_be_stored_is_reported_not_swallowed(tmp_path) -> None:
    from finepdfs.domain.chunks import chunk_page_records

    record = PageRecord(
        doc_id="a" * 16,
        source_url="https://a.example/one.pdf",
        digest="1" * 64,
        page_number=1,
        text="Common harbour words",
        engine="pymupdf@native",
    )
    good = chunk_page_records((record,), language="eng")[0]
    collision = replace(good, text="different words under the same identity")
    index_path = tmp_path / "index.db"

    report = _reindex_chunks(index_path, (good, collision))

    assert not report.ok
    assert report.upserted == 1
    assert len(report.failures) == 1
    assert report.failures[0].chunk_id == good.chunk_id
    assert "same chunk_id" in report.failures[0].reason
    assert _search_chunks(index_path, (good, collision), "harbour").available is False, (
        "a partial rebuild stays stale on purpose: its rows are not evidence the declared set is covered"
    )
    _reindex_chunks(index_path, (good,))
    assert len(good_chunks_hits(index_path, good)) == 1, "a clean rebuild makes the stored chunk queryable"


def test_a_replacement_is_not_searchable_until_the_index_is_rebuilt(monkeypatch, tmp_path) -> None:
    """ADR-0008: the index is only as fresh as the last rebuild; stale evidence is never served."""
    first_body = FIXTURE.read_bytes()
    second_body = first_body + b"\n% replaced bytes\n"
    _write_corpus(tmp_path / "corpus", {URL: first_body})
    engine = ControlledEngine(
        {
            content_digest(first_body): (PageText(1, "Bravo paragraph about buoys and wind at dawn."),),
            content_digest(second_body): (PageText(1, "Delta paragraph, the only replacement page here."),),
        }
    )
    runtime = _runtime(tmp_path, engine, ControlledLanguages())
    _run(monkeypatch, tmp_path, runtime)
    index_path = tmp_path / "index.db"
    _reindex(runtime.out_dir, index_path)
    assert len(_search(runtime.out_dir, index_path, "Bravo").hits) == 1

    _write_corpus(tmp_path / "corpus", {URL: second_body})
    _run(monkeypatch, tmp_path, runtime)

    stale = _search(runtime.out_dir, index_path, "Bravo")
    assert stale.available is False, "the index no longer matches the declared chunks"
    assert stale.hits == (), "no stale text is returned while the index is out of date"
    assert "index-search" in stale.reason
    assert _search(runtime.out_dir, index_path, "Delta").available is False, "a fresh chunk is not served from a stale index"

    _reindex(runtime.out_dir, index_path)
    assert _search(runtime.out_dir, index_path, "Bravo").hits == ()
    delta = _search(runtime.out_dir, index_path, "Delta")
    assert delta.available is True
    assert [hit.chunk.page_number for hit in delta.hits] == [1]
    assert delta.hits[0].chunk.digest == content_digest(second_body)


def test_a_removal_is_not_searchable_until_the_index_is_rebuilt(monkeypatch, tmp_path) -> None:
    second_url = "https://example.org/second.pdf"
    first_body = FIXTURE.read_bytes()
    second_body = first_body + b"\n% a second, unrelated source\n"
    _write_corpus(tmp_path / "corpus", {URL: first_body, second_url: second_body})
    engine = ControlledEngine(
        {
            content_digest(first_body): (PageText(1, "Alpine meteorology over the northern range."),),
            content_digest(second_body): (PageText(1, "Brighton buoy reports swell and wind at dawn."),),
        }
    )
    runtime = _runtime(tmp_path, engine, ControlledLanguages())
    _run(monkeypatch, tmp_path, runtime)
    index_path = tmp_path / "index.db"
    _reindex(runtime.out_dir, index_path)

    _keep_only(tmp_path / "corpus", {second_url})
    _run(monkeypatch, tmp_path, runtime)

    stale = _search(runtime.out_dir, index_path, "meteorology")
    assert stale.available is False
    assert stale.hits == (), "a removed Document cannot supply stale evidence"
    assert "index-search" in stale.reason

    _reindex(runtime.out_dir, index_path)
    assert _search(runtime.out_dir, index_path, "meteorology").hits == ()
    assert len(_search(runtime.out_dir, index_path, "Brighton").hits) == 1


def test_reading_an_index_never_opens_it_read_write(tmp_path) -> None:
    """A reader that created or repaired the database would be a second writer (ADR-0008)."""
    from finepdfs.domain.search import SearchQuery, SearchUnavailable
    from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex

    empty = tmp_path / "empty" / "search.db"
    empty.parent.mkdir()
    empty.write_bytes(b"")

    with pytest.raises(SearchUnavailable, match="empty"):
        SqliteFtsSearchIndex(empty).search(SearchQuery(text="alpine"))
    assert empty.read_bytes() == b"", "a 0-byte index stays 0 bytes"

    missing = tmp_path / "missing" / "search.db"
    with pytest.raises(SearchUnavailable, match="no search index"):
        SqliteFtsSearchIndex(missing).state()
    assert not missing.exists(), "reading must not create an index"
    assert not missing.parent.exists(), "reading must not create the index directory"

    schema_less = tmp_path / "schema-less.db"
    sqlite3.connect(schema_less).execute("CREATE TABLE unrelated (x TEXT)").connection.close()
    with pytest.raises(SearchUnavailable, match="index-search"):
        SqliteFtsSearchIndex(schema_less).state()
    with pytest.raises(SearchUnavailable):
        SqliteFtsSearchIndex(schema_less).search(SearchQuery(text="alpine"))
    tables = sqlite3.connect(schema_less).execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
    sqlite3.connect(schema_less).close()
    assert tables == [("unrelated",)], "a reader must not create or migrate schema"


def test_chunks_are_declared_only_when_search_is_enabled(monkeypatch, tmp_path) -> None:
    """The spec says "chunks when search is enabled": a plain ingestion run declares no chunk export."""
    body = FIXTURE.read_bytes()
    _write_corpus(tmp_path / "corpus", {URL: body})
    engine = ControlledEngine(
        {content_digest(body): (PageText(1, "Alpine meteorology over the northern range."),)}
    )
    enabled = _runtime(tmp_path, engine, ControlledLanguages())
    _run(monkeypatch, tmp_path, enabled)
    assert _chunks_path(tmp_path / "out", URL).is_file()
    assert _pages_path(tmp_path / "out", URL).is_file(), "pages are ingestion output, not search output"

    plain = _runtime(tmp_path, engine, ControlledLanguages(), search=False)
    _run(monkeypatch, tmp_path, plain)

    assert not _chunks_path(tmp_path / "out", URL).exists(), (
        "a run without search retracts the chunks it no longer declares"
    )
    assert _pages_path(tmp_path / "out", URL).is_file()
    assert _reindex(tmp_path / "out", tmp_path / "index.db").chunks == 0
    assert _search(tmp_path / "out", tmp_path / "index.db", "Alpine").available is False


def test_the_search_setting_comes_from_the_documented_variable(monkeypatch, tmp_path) -> None:
    from finepdfs.infrastructure.cocoindex.runtime import runtime_from_environment

    assert runtime_from_environment({"FINEPDFS_SEARCH": "1"}).search_enabled is True
    assert runtime_from_environment({"FINEPDFS_SEARCH": "true"}).search_enabled is True
    assert runtime_from_environment({"FINEPDFS_SEARCH": ""}).search_enabled is False
    assert runtime_from_environment({"FINEPDFS_SEARCH": "0"}).search_enabled is False
