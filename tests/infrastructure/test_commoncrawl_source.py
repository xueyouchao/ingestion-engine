"""How the live host's answers are turned into records, including the awkward ones."""

from __future__ import annotations

import io
import urllib.error

import pytest

from finepdfs.domain.crawl import CrawlQuery
from finepdfs.infrastructure.commoncrawl.source import CommonCrawlError, CommonCrawlSource
from finepdfs.infrastructure.commoncrawl.warc import WarcParseError

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
    with pytest.raises(WarcParseError):
        _source(opener).fetch(record)  # the payload is not a WARC record, but the request was made

    assert seen["range"] == "bytes=100-150"
    assert seen["url"] == "https://data.commoncrawl.org/crawl-data/x.warc.gz"
    assert "finepdfs" in seen["agent"]


class _Response(io.BytesIO):
    """A stand-in for urllib's response, carrying the status and headers the adapter inspects."""

    def __init__(self, body: bytes, status: int = 206, headers: dict | None = None) -> None:
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _record():
    from finepdfs.domain.crawl import CrawlRecord

    return CrawlRecord(
        url="https://example.com/doc.pdf",
        crawl_id="CC-MAIN-2026-39",
        warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
        offset=100,
        length=51,
    )


def test_a_host_that_ignores_range_is_an_error_not_a_different_document() -> None:
    def opener(request, timeout=None):
        return _Response(b"the whole shard, not the range that was asked for", status=200)

    with pytest.raises(CommonCrawlError) as raised:
        _source(opener).fetch(_record())

    assert "ignored the Range" in str(raised.value)


def test_a_content_range_that_does_not_match_the_request_is_an_error() -> None:
    def opener(request, timeout=None):
        return _Response(b"x" * 10, status=206, headers={"Content-Range": "bytes 0-9/19355"})

    with pytest.raises(CommonCrawlError) as raised:
        _source(opener).fetch(_record())

    assert "Content-Range" in str(raised.value)


def test_a_permanent_client_error_is_not_retried() -> None:
    calls = []

    def opener(request, timeout=None):
        calls.append(request.full_url)
        raise _http_error(400, b"<html>bad filter</html>")

    with pytest.raises(CommonCrawlError):
        _source(opener, attempts=3).list_records(QUERY)

    assert len(calls) == 1, "a 400 will still be a 400 in three seconds"


def test_a_200_that_is_not_an_index_answer_is_an_error_not_an_empty_corpus() -> None:
    def opener(request, timeout=None):
        return _Response(b"<html><body>503 backend unavailable</body></html>", status=200)

    with pytest.raises(CommonCrawlError) as raised:
        _source(opener).list_records(QUERY)

    assert "without a single usable record" in str(raised.value)
