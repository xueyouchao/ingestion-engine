"""How the live host's answers are turned into records, including the awkward ones."""

from __future__ import annotations

import io
import urllib.error

import pytest

from finepdfs.domain.crawl import CrawlQuery
from finepdfs.infrastructure.commoncrawl.source import CommonCrawlError, CommonCrawlSource

QUERY = CrawlQuery(crawl_id="CC-MAIN-2026-39", domain="example.com")


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://index.commoncrawl.org/", code, "error", {}, io.BytesIO(body))


def _source(opener, attempts: int = 1) -> CommonCrawlSource:
    return CommonCrawlSource(opener=opener, attempts=attempts, pause_seconds=0)


def test_a_domain_with_no_captures_is_an_empty_result_not_a_failure() -> None:
    """The live index answers 404 with a JSON message for a domain it never saw."""

    def opener(request, timeout=None):
        raise _http_error(404, b'{"message": "No Captures found for: example.com"}')

    assert _source(opener).list_records(QUERY) == []


def test_a_gateway_error_is_retried_and_then_reported() -> None:
    calls = []

    def opener(request, timeout=None):
        calls.append(request.full_url)
        raise _http_error(502, b"<html>502 Bad Gateway</html>")

    with pytest.raises(CommonCrawlError) as raised:
        _source(opener, attempts=3).list_records(QUERY)

    assert len(calls) == 3
    assert "could not read" in str(raised.value)


def test_a_range_request_asks_for_exactly_the_record_it_wants() -> None:
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def opener(request, timeout=None):
        seen["range"] = request.get_header("Range")
        seen["url"] = request.full_url
        seen["agent"] = request.get_header("User-agent")
        return Response(b"payload")

    from finepdfs.domain.crawl import CrawlRecord

    record = CrawlRecord(
        url="https://example.com/doc.pdf",
        crawl_id="CC-MAIN-2026-39",
        warc_filename="crawl-data/x.warc.gz",
        offset=100,
        length=51,
    )
    with pytest.raises(Exception):
        _source(opener).fetch(record)  # the payload is not a WARC record, but the request was made

    assert seen["range"] == "bytes=100-150"
    assert seen["url"] == "https://data.commoncrawl.org/crawl-data/x.warc.gz"
    assert "finepdfs" in seen["agent"]
