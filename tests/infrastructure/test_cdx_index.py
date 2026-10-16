"""Adapter contract seam: Common Crawl index responses to Crawl Records."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

import pytest

from finepdfs.domain.crawl import CrawlQuery, CrawlRecord
from finepdfs.infrastructure.commoncrawl.cdx import build_index_url, parse_index_response

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_parses_a_recorded_index_response_into_crawl_records() -> None:
    response = (FIXTURES / "cdx_response.jsonl").read_text()

    records = parse_index_response(response)

    assert len(records) == 12
    first = records[0]
    assert isinstance(first, CrawlRecord)
    assert first.url.startswith("https://")
    assert first.mime_type == "application/pdf"
    assert first.status == 200
    assert first.offset > 0 and first.length > 0
    assert first.crawl_id == "CC-MAIN-2026-39"
    assert first.warc_url.startswith("https://data.commoncrawl.org/crawl-data/")


def test_ignores_lines_that_are_not_records() -> None:
    response = '{"message": "No Captures found for: example.com"}\n' + (FIXTURES / "cdx_response.jsonl").read_text()

    assert len(parse_index_response(response)) == 12
    assert parse_index_response('{"message": "No Captures found for: example.com"}') == []


def test_index_url_carries_the_query_knobs() -> None:
    url = build_index_url(CrawlQuery(crawl_id="CC-MAIN-2026-39", domain="noaa.gov", limit=25))

    base, _, raw_query = url.partition("?")
    parameters = parse_qs(raw_query)

    assert base == "https://index.commoncrawl.org/CC-MAIN-2026-39-index"
    assert parameters["url"] == ["noaa.gov"]
    assert parameters["matchType"] == ["domain"]
    assert parameters["filter"] == ["mime:application/pdf", "status:200"]
    assert parameters["output"] == ["json"]
    assert parameters["limit"] == ["25"]


def test_a_query_rejects_an_empty_domain() -> None:
    with pytest.raises(ValueError):
        CrawlQuery(crawl_id="CC-MAIN-2026-39", domain="  ")


def test_rows_missing_a_field_are_skipped_rather_than_fatal() -> None:
    """One malformed row must not cost the rest of the index answer, or the whole run."""
    response = "\n".join(
        [
            '{"url": "https://a.example/one.pdf", "filename": "crawl-data/CC-MAIN-2026-39/x.warc.gz",'
            ' "offset": "100", "length": "500"}',
            '{"url": "https://a.example/two.pdf", "filename": "crawl-data/CC-MAIN-2026-39/x.warc.gz",'
            ' "offset": "200"}',
            '{"filename": "crawl-data/CC-MAIN-2026-39/x.warc.gz", "offset": "300", "length": "500"}',
            '{"url": "https://a.example/three.pdf", "filename": "crawl-data/CC-MAIN-2026-39/x.warc.gz",'
            ' "offset": "not a number", "length": "500"}',
            '{"url": "https://a.example/four.pdf", "filename": "crawl-data/CC-MAIN-2026-39/x.warc.gz",'
            ' "offset": "400", "length": "600"}',
            "{ this line is not json at all",
        ]
    )

    records = parse_index_response(response)

    assert [record.url for record in records] == ["https://a.example/one.pdf", "https://a.example/four.pdf"]
    assert records[0].offset == 100 and records[0].length == 500
    assert records[0].crawl_id == "CC-MAIN-2026-39"
