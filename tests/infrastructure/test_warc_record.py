"""Adapter contract seam: WARC record bytes to PDF payload.

A Common Crawl range request returns one WARC record, gzip-compressed as a single member, holding an
HTTP response whose body is the PDF. Getting this mapping wrong is the difference between a corpus and
a directory of garbage, and it needs no network to check.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from finepdfs.infrastructure.commoncrawl.warc import WarcParseError, parse_pdf_payload

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _warc_record(
    body: bytes,
    content_type: str = "application/pdf",
    content_encoding: str = "",
    content_length: int | None = None,
    warc_truncated: str = "",
) -> bytes:
    """Build one WARC record the way Common Crawl lays it out, for negative cases."""
    encoding_header = f"Content-Encoding: {content_encoding}\r\n" if content_encoding else ""
    length_header = f"Content-Length: {content_length}\r\n" if content_length is not None else ""
    truncated_header = f"WARC-Truncated: {warc_truncated}\r\n" if warc_truncated else ""
    http_body = gzip.compress(body) if content_encoding == "gzip" else body
    record = (
        b"WARC/1.0\r\n"
        b"WARC-Type: response\r\n"
        b"WARC-Target-URI: https://example.org/doc.pdf\r\n"
        b"Content-Length: %d\r\n" % len(http_body)
        + truncated_header.encode()
        + b"\r\n"
        + b"HTTP/1.1 200 OK\r\n"
        + f"Content-Type: {content_type}\r\n".encode()
        + encoding_header.encode()
        + length_header.encode()
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


def test_the_recorded_fixture_agrees_with_the_metadata_captured_beside_it() -> None:
    """record.json holds the numbers the live capture saw; the parser has to agree with them."""
    metadata = json.loads((FIXTURES / "record.json").read_text())
    record_bytes = (FIXTURES / "warc_record.bin").read_bytes()

    payload = parse_pdf_payload(record_bytes, expected_length=int(metadata["length"]))

    assert len(record_bytes) == int(metadata["length"])
    assert len(payload) == int(metadata["declared_content_length"])
    assert payload == (FIXTURES / "warc_payload.bin").read_bytes()


def test_a_range_that_came_back_the_wrong_size_is_rejected() -> None:
    """A host that ignores Range hands back a different region than the one that was asked for."""
    record_bytes = (FIXTURES / "warc_record.bin").read_bytes()

    with pytest.raises(WarcParseError) as raised:
        parse_pdf_payload(record_bytes, expected_length=len(record_bytes) - 1)

    assert "asked for" in str(raised.value)


def test_a_gzip_member_that_stops_in_the_middle_is_rejected() -> None:
    truncated = gzip.compress(_warc_record(b"%PDF-1.7 hello"))[:20]

    with pytest.raises(WarcParseError) as raised:
        parse_pdf_payload(truncated)

    assert "stops in the middle" in str(raised.value)


def test_two_gzip_members_are_rejected_rather_than_concatenated() -> None:
    """What the whole shard looks like: the requested record and the next one, glued together."""
    first = _warc_record(b"%PDF-1.7 first")
    second = _warc_record(b"%PDF-1.7 second")

    with pytest.raises(WarcParseError) as raised:
        parse_pdf_payload(first + second)

    assert "more than one gzip member" in str(raised.value)


def test_trailing_bytes_that_belong_to_the_pdf_survive() -> None:
    """Content-Length delimits the body, so a PDF that itself ends in CRLF keeps those bytes."""
    body = b"%PDF-1.7 ends with blank lines\r\n\r\n"

    payload = parse_pdf_payload(_warc_record(body, content_length=len(body)))

    assert payload == body


def test_a_body_shorter_than_its_own_content_length_is_rejected() -> None:
    record = _warc_record(b"%PDF-1.7 hello", content_length=999)

    with pytest.raises(WarcParseError) as raised:
        parse_pdf_payload(record)

    assert "payload bytes" in str(raised.value)


def test_a_record_common_crawl_marked_truncated_is_rejected() -> None:
    """Common Crawl records the truncation in the WARC header; a partial document must not pass."""
    record = _warc_record(b"%PDF-1.7 half a document", warc_truncated="length")

    with pytest.raises(WarcParseError) as raised:
        parse_pdf_payload(record)

    assert "truncated" in str(raised.value).lower()
