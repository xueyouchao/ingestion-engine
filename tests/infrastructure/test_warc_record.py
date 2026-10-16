"""Adapter contract seam: WARC record bytes to PDF payload.

A Common Crawl range request returns one WARC record, gzip-compressed as a single member, holding an
HTTP response whose body is the PDF. Getting this mapping wrong is the difference between a corpus and
a directory of garbage, and it needs no network to check.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from finepdfs.infrastructure.commoncrawl.warc import WarcParseError, parse_pdf_payload

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _warc_record(body: bytes, content_type: str = "application/pdf", content_encoding: str = "") -> bytes:
    """Build one WARC record the way Common Crawl lays it out, for negative cases."""
    encoding_header = f"Content-Encoding: {content_encoding}\r\n" if content_encoding else ""
    http_body = gzip.compress(body) if content_encoding == "gzip" else body
    record = (
        b"WARC/1.0\r\n"
        b"WARC-Type: response\r\n"
        b"WARC-Target-URI: https://example.org/doc.pdf\r\n"
        b"Content-Length: %d\r\n" % len(http_body)
        + b"\r\n"
        + b"HTTP/1.1 200 OK\r\n"
        + f"Content-Type: {content_type}\r\n".encode()
        + encoding_header.encode()
        + b"\r\n"
        + http_body
        + b"\r\n\r\n"
    )
    return gzip.compress(record)


def test_extracts_the_pdf_payload_from_a_real_common_crawl_record() -> None:
    record_bytes = (FIXTURES / "warc_record.bin").read_bytes()
    expected_payload = (FIXTURES / "warc_payload.bin").read_bytes()

    payload = parse_pdf_payload(record_bytes)

    assert payload.startswith(b"%PDF")
    assert payload == expected_payload


def test_decodes_a_gzipped_http_body() -> None:
    payload = parse_pdf_payload(_warc_record(b"%PDF-1.7 hello", content_encoding="gzip"))

    assert payload == b"%PDF-1.7 hello"


def test_rejects_bytes_that_are_not_a_warc_record() -> None:
    with pytest.raises(WarcParseError):
        parse_pdf_payload(gzip.compress(b"this is not a warc record at all"))


def test_rejects_a_record_whose_payload_is_not_a_pdf() -> None:
    record = _warc_record(b"<html><body>not a pdf</body></html>", content_type="text/html")

    with pytest.raises(WarcParseError) as raised:
        parse_pdf_payload(record)

    assert "pdf" in str(raised.value).lower()
