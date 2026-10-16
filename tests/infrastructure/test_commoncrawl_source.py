"""How the live host's answers are turned into records, including the awkward ones."""

from __future__ import annotations

import http.client
import io
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

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


class _StubHost:
    """A real HTTP host that answers every request, and can hang up part way through the body it promised.

    ``Content-Length`` always declares the whole body; ``cut_after`` decides how many bytes are actually
    paid before the connection closes. Reading fewer bytes than promised is what makes the client raise
    ``http.client.IncompleteRead`` — the failure this file is about. A fake opener raising that exception
    would prove only that the retry tuple catches the class; a real socket proves the failure mode.
    """

    def __init__(self, body: bytes, *, cut_after: int | None = None) -> None:
        self._body = body
        self._cut_after = cut_after
        self.requests: list[str] = []
        host = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                host.requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(len(host._body)))
                self.end_headers()
                self.wfile.write(host._body[: host._cut_after])
                self.wfile.flush()
                self.close_connection = host._cut_after is not None

            def log_message(self, *_) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/doc"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _to_stub(url: str):
    """The request the source built, addressed at the stub host instead of the live one."""

    def opener(request, timeout=None):
        request.full_url = url
        return urllib.request.urlopen(request, timeout=timeout)

    return opener


def test_a_transfer_cut_mid_body_is_retried_rather_than_lost() -> None:
    """A cut mid-body is a transport failure, so it costs a retry — not the whole document."""

    with _StubHost(b"x" * 64, cut_after=8) as host, pytest.raises(CommonCrawlError) as raised:
        _source(_to_stub(host.url), attempts=3).list_records(QUERY)

    assert len(host.requests) == 3, "the cut is worth the same three attempts as any other failure"
    assert isinstance(raised.value.__cause__, http.client.IncompleteRead)


def test_a_cut_that_persists_is_a_reason_not_a_partial_document() -> None:
    """Past the attempts the record fails with a reason; no half-read bytes reach the caller."""

    with _StubHost(b"x" * 64, cut_after=8) as host, pytest.raises(CommonCrawlError) as raised:
        _source(_to_stub(host.url), attempts=2).fetch(_record())

    assert len(host.requests) == 2
    assert "could not read" in str(raised.value)
    assert isinstance(raised.value.__cause__, http.client.IncompleteRead)


def test_a_whole_answer_over_a_real_socket_is_read_exactly_once() -> None:
    """Retrying a cut must not turn a good answer into a retry, nor a retry into a second download."""

    body = (
        b'{"url": "https://example.com/a.pdf", "filename": "crawl-data/CC-MAIN-2026-39/x.warc.gz", '
        b'"offset": "100", "length": "51"}\n'
    )
    with _StubHost(body) as host:
        records = _source(_to_stub(host.url), attempts=3).list_records(QUERY)

    assert [record.url for record in records] == ["https://example.com/a.pdf"]
    assert len(host.requests) == 1
