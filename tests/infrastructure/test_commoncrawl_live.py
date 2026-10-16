"""The only tests that touch the network. Run with: pytest -m network."""

from __future__ import annotations

import pytest

from finepdfs.domain.crawl import CrawlQuery
from finepdfs.infrastructure.commoncrawl.cdx import latest_crawl_id
from finepdfs.infrastructure.commoncrawl.source import CommonCrawlSource

pytestmark = pytest.mark.network


def test_newsest_crawl_is_discoverable_from_the_collection_list() -> None:
    source = CommonCrawlSource()

    crawl_id = source.latest_crawl_id()

    assert crawl_id.startswith("CC-MAIN-")


def test_a_real_pdf_comes_back_from_the_live_data_host() -> None:
    """The data host occasionally answers 503 for a single segment, so try a few records."""
    source = CommonCrawlSource()
    query = CrawlQuery(crawl_id=source.latest_crawl_id(), domain="noaa.gov", limit=6)

    records = source.list_records(query)
    assert records, "the live index returned no PDFs for a domain that has many"

    payloads = []
    for record in records[:5]:
        try:
            payloads.append(source.fetch(record))
            break
        except Exception:  # a single unavailable segment is not a failed pipeline
            continue

    assert payloads, "no record of the first five could be fetched from the live data host"
    assert payloads[0].startswith(b"%PDF")
    assert len(payloads[0]) > 1000


def test_collection_list_parser_picks_the_newest_crawl() -> None:
    sample = [
        {"id": "CC-MAIN-2026-39", "name": "September 2026 Index"},
        {"id": "CC-MAIN-2026-30", "name": "August 2026 Index"},
    ]

    assert latest_crawl_id(sample) == "CC-MAIN-2026-39"


def test_collection_list_parser_rejects_a_list_without_crawls() -> None:
    with pytest.raises(ValueError):
        latest_crawl_id([{"id": "other-index"}])
