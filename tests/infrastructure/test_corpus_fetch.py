"""The corpus fetch step: one command, real files on disk, resumable.

Driven through the CrawlSource port with a hand-written fake, so these tests need no network.
"""

from __future__ import annotations

import hashlib

from finepdfs.domain.crawl import CrawlQuery, CrawlRecord
from finepdfs.infrastructure.corpus import fetch as fetch_module
from finepdfs.infrastructure.corpus.fetch import fetch_corpus
from finepdfs.infrastructure.corpus.manifest import CorpusManifest

PDF_ONE = b"%PDF-1.7 first document"
PDF_TWO = b"%PDF-1.7 second document"


class FakeCrawlSource:
    """A Crawl Source that answers from dictionaries, and can be made to fail."""

    def __init__(self, records, payloads, failing_urls=()):
        self._records = list(records)
        self._payloads = dict(payloads)
        self._failing = set(failing_urls)
        self.fetch_calls = []

    def list_records(self, query):
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
