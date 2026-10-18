"""The corpus fetch step: one command, real files on disk, resumable.

Driven through the CrawlSource port with a hand-written fake, so these tests need no network.
"""

from __future__ import annotations

import hashlib

from finepdfs.domain.crawl import CrawlQuery, CrawlRecord
from finepdfs.infrastructure.corpus import fetch as fetch_module
from finepdfs.infrastructure.corpus.fetch import fetch_corpus, restore_corpus
from finepdfs.infrastructure.corpus.index import CorpusIndex
from finepdfs.infrastructure.corpus.manifest import (
    CorpusEntry,
    CorpusManifest,
    relative_pdf_path,
)

PDF_ONE = b"%PDF-1.7 first document"
PDF_TWO = b"%PDF-1.7 second document"


class FakeCrawlSource:
    """A Crawl Source that answers from dictionaries, and can be made to fail."""

    def __init__(self, records, payloads, failing_urls=()):
        self._records = list(records)
        self._payloads = dict(payloads)
        self._failing = set(failing_urls)
        self.fetch_calls = []
        self.list_calls = []

    def list_records(self, query):
        self.list_calls.append(query)
        return list(self._records)

    def fetch(self, record):
        self.fetch_calls.append(record.url)
        if record.url in self._failing:
            raise RuntimeError("the data host refused this record")
        return self._payloads[record.url]


def _record(url: str, offset: int = 100) -> CrawlRecord:
    return CrawlRecord(
        url=url,
        crawl_id="CC-MAIN-2026-39",
        warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
        offset=offset,
        length=500,
    )


def _query() -> CrawlQuery:
    return CrawlQuery(crawl_id="CC-MAIN-2026-39", domain="noaa.gov", limit=10)


def test_writes_each_pdf_named_by_digest_and_records_a_manifest(tmp_path):
    source = FakeCrawlSource(
        [_record("https://a.example/one.pdf"), _record("https://a.example/two.pdf")],
        {"https://a.example/one.pdf": PDF_ONE, "https://a.example/two.pdf": PDF_TWO},
    )

    report = fetch_corpus(source, _query(), corpus_dir=tmp_path)

    payloads = {"https://a.example/one.pdf": PDF_ONE, "https://a.example/two.pdf": PDF_TWO}

    manifest = CorpusManifest.load(tmp_path / "manifest.json")
    assert report.fetched == 2 and report.skipped == 0 and report.failed == 0
    assert len(manifest.entries) == 2
    for entry in manifest.entries:
        assert entry.file == f"pdfs/{entry.digest}.pdf"
        assert (tmp_path / entry.file).read_bytes() == payloads[entry.url]
        assert entry.bytes == len(payloads[entry.url])
        assert entry.digest == hashlib.sha256(payloads[entry.url]).hexdigest()


def test_the_same_pdf_from_two_crawl_records_is_stored_once(tmp_path):
    source = FakeCrawlSource(
        [_record("https://a.example/one.pdf"), _record("https://mirror.example/one.pdf")],
        {"https://a.example/one.pdf": PDF_ONE, "https://mirror.example/one.pdf": PDF_ONE},
    )

    report = fetch_corpus(source, _query(), corpus_dir=tmp_path)

    assert report.fetched == 2
    written = sorted(p.name for p in (tmp_path / "pdfs").iterdir())
    assert written == [f"{hashlib.sha256(PDF_ONE).hexdigest()}.pdf"]


def test_a_second_run_refetches_nothing(tmp_path):
    source = FakeCrawlSource([_record("https://a.example/one.pdf")], {"https://a.example/one.pdf": PDF_ONE})
    fetch_corpus(source, _query(), corpus_dir=tmp_path)

    second = fetch_corpus(source, _query(), corpus_dir=tmp_path)

    assert second.fetched == 0 and second.skipped == 1
    assert source.fetch_calls == ["https://a.example/one.pdf"]


def test_one_failing_record_does_not_stop_the_run(tmp_path):
    records = [_record("https://a.example/bad.pdf"), _record("https://a.example/good.pdf")]
    source = FakeCrawlSource(
        records,
        {"https://a.example/good.pdf": PDF_TWO},
        failing_urls={"https://a.example/bad.pdf"},
    )

    report = fetch_corpus(source, _query(), corpus_dir=tmp_path)

    assert report.fetched == 1 and report.failed == 1
    assert "bad.pdf" in report.failures[0].url
    assert "refused" in report.failures[0].reason
    assert len(CorpusManifest.load(tmp_path / "manifest.json").entries) == 1


def test_a_manifest_records_the_query_that_produced_it(tmp_path):
    source = FakeCrawlSource([_record("https://a.example/one.pdf")], {"https://a.example/one.pdf": PDF_ONE})

    fetch_corpus(source, _query(), corpus_dir=tmp_path)

    manifest = CorpusManifest.load(tmp_path / "manifest.json")
    assert manifest.domain == "noaa.gov"
    assert manifest.crawl_id == "CC-MAIN-2026-39"


def test_an_empty_query_result_is_reported_not_silently_ignored(tmp_path):
    report = fetch_corpus(FakeCrawlSource([], {}), _query(), corpus_dir=tmp_path)

    assert report.fetched == 0 and report.failed == 0
    assert report.records_seen == 0


def test_a_truncated_pdf_on_disk_is_fetched_again(tmp_path):
    """What a crash between open and close leaves behind must not be trusted forever."""
    source = FakeCrawlSource([_record("https://a.example/one.pdf")], {"https://a.example/one.pdf": PDF_ONE})
    fetch_corpus(source, _query(), corpus_dir=tmp_path)
    stored = next((tmp_path / "pdfs").iterdir())
    stored.write_bytes(stored.read_bytes()[:5])

    report = fetch_corpus(source, _query(), corpus_dir=tmp_path)

    assert report.fetched == 1 and report.skipped == 0
    assert stored.read_bytes() == PDF_ONE


def test_a_pdf_deleted_from_disk_is_fetched_again(tmp_path):
    source = FakeCrawlSource([_record("https://a.example/one.pdf")], {"https://a.example/one.pdf": PDF_ONE})
    fetch_corpus(source, _query(), corpus_dir=tmp_path)
    for stored in (tmp_path / "pdfs").iterdir():
        stored.unlink()

    report = fetch_corpus(source, _query(), corpus_dir=tmp_path)

    assert report.fetched == 1 and report.skipped == 0
    assert len(list((tmp_path / "pdfs").iterdir())) == 1


def _pinned_entry(url: str, payload: bytes, offset: int = 100) -> CorpusEntry:
    digest = hashlib.sha256(payload).hexdigest()
    return CorpusEntry(
        url=url,
        digest=digest,
        file=relative_pdf_path(digest),
        bytes=len(payload),
        crawl_id="CC-MAIN-2026-39",
        warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
        offset=offset,
        length=len(payload),
    )


def test_restore_replays_the_pinned_manifest_without_querying_the_index(tmp_path):
    stale = _pinned_entry("https://old.example/stale.pdf", b"%PDF-1.7 stale")
    CorpusManifest(crawl_id="CC-MAIN-2025-01", domain="old.example", entries=[stale]).save(tmp_path / "manifest.json")
    pinned = CorpusManifest(
        crawl_id="CC-MAIN-2026-39",
        domain="a.example",
        entries=[
            _pinned_entry("https://a.example/one.pdf", PDF_ONE),
            _pinned_entry("https://a.example/two.pdf", PDF_TWO, offset=900),
        ],
    )
    source = FakeCrawlSource(
        [],
        {"https://a.example/one.pdf": PDF_ONE, "https://a.example/two.pdf": PDF_TWO},
    )

    report = restore_corpus(source, pinned, corpus_dir=tmp_path)

    assert report.records_seen == 2 and report.fetched == 2 and report.skipped == 0 and report.failed == 0
    assert source.list_calls == [], "restoration must replay the manifest, not ask the index for the latest crawl"
    assert source.fetch_calls == ["https://a.example/one.pdf", "https://a.example/two.pdf"]
    manifest = CorpusManifest.load(tmp_path / "manifest.json")
    assert manifest == pinned
    for entry in pinned.entries:
        assert (tmp_path / entry.file).read_bytes() in (PDF_ONE, PDF_TWO)


def test_a_second_restore_verifies_local_bytes_and_fetches_nothing(tmp_path):
    pinned = CorpusManifest(
        crawl_id="CC-MAIN-2026-39",
        domain="a.example",
        entries=[_pinned_entry("https://a.example/one.pdf", PDF_ONE)],
    )
    source = FakeCrawlSource([], {"https://a.example/one.pdf": PDF_ONE})
    restore_corpus(source, pinned, corpus_dir=tmp_path)

    second = restore_corpus(source, pinned, corpus_dir=tmp_path)

    assert second.fetched == 0 and second.skipped == 1 and second.failed == 0
    assert source.fetch_calls == ["https://a.example/one.pdf"]


def test_restore_refetches_local_bytes_that_do_not_match_the_pinned_digest(tmp_path):
    pinned = CorpusManifest(
        crawl_id="CC-MAIN-2026-39",
        domain="a.example",
        entries=[_pinned_entry("https://a.example/one.pdf", PDF_ONE)],
    )
    source = FakeCrawlSource([], {"https://a.example/one.pdf": PDF_ONE})
    restore_corpus(source, pinned, corpus_dir=tmp_path)
    (tmp_path / pinned.entries[0].file).write_bytes(PDF_TWO)

    report = restore_corpus(source, pinned, corpus_dir=tmp_path)

    assert report.fetched == 1 and report.skipped == 0 and report.failed == 0
    assert (tmp_path / pinned.entries[0].file).read_bytes() == PDF_ONE


def test_restore_fails_a_payload_that_does_not_match_the_pinned_digest(tmp_path):
    pinned = CorpusManifest(
        crawl_id="CC-MAIN-2026-39",
        domain="a.example",
        entries=[_pinned_entry("https://a.example/one.pdf", PDF_ONE)],
    )
    source = FakeCrawlSource([], {"https://a.example/one.pdf": PDF_TWO})

    report = restore_corpus(source, pinned, corpus_dir=tmp_path)

    assert report.fetched == 0 and report.failed == 1
    assert "pinned" in report.failures[0].reason
    assert not (tmp_path / pinned.entries[0].file).exists(), "bytes that are not the pinned Document are not stored"


def test_a_missing_remote_record_stays_declared_instead_of_becoming_a_source_deletion(tmp_path):
    """An unavailable URL is that record's failure, not permission to drop it from the target state."""
    one = "https://a.example/one.pdf"
    gone = "https://a.example/gone.pdf"
    pinned = CorpusManifest(
        crawl_id="CC-MAIN-2026-39",
        domain="a.example",
        entries=[_pinned_entry(one, PDF_ONE), _pinned_entry(gone, PDF_TWO, offset=900)],
    )
    source = FakeCrawlSource([], {one: PDF_ONE}, failing_urls={gone})

    report = restore_corpus(source, pinned, corpus_dir=tmp_path)

    assert report.fetched == 1 and report.failed == 1
    assert gone in report.failures[0].url
    assert CorpusManifest.load(tmp_path / "manifest.json").urls == {one, gone}
    assert {document.url for document in CorpusIndex(tmp_path).documents()} == {one, gone}


def test_a_disk_that_fills_up_is_one_record_s_problem(tmp_path, monkeypatch):
    records = [_record("https://a.example/one.pdf"), _record("https://a.example/two.pdf")]
    source = FakeCrawlSource(
        records,
        {"https://a.example/one.pdf": PDF_ONE, "https://a.example/two.pdf": PDF_TWO},
    )
    real_store = fetch_module._store

    def failing_store(corpus_dir, digest, payload):
        if payload == PDF_TWO:
            raise OSError("No space left on device")
        real_store(corpus_dir, digest, payload)

    monkeypatch.setattr(fetch_module, "_store", failing_store)

    report = fetch_corpus(source, _query(), corpus_dir=tmp_path)

    assert report.fetched == 1 and report.failed == 1
    assert "No space left" in report.failures[0].reason
    manifest = CorpusManifest.load(tmp_path / "manifest.json")
    assert [entry.url for entry in manifest.entries] == ["https://a.example/one.pdf"]
