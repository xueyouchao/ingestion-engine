"""Reading one Common Crawl WARC record out of a byte range.

A ranged read of a .warc.gz file returns a self-contained gzip member, so one record can be decoded
without the rest of the shard. Inside it sits a WARC header block, then an HTTP response whose body is
the PDF. The record is followed by a blank-line separator that belongs to the WARC framing, not to the
PDF, so it is removed.
"""

from __future__ import annotations

import gzip
import zlib

_HEADER_END = b"\r\n\r\n"
_GZIP_MAGIC = b"\x1f\x8b"
_PDF_MAGIC = b"%PDF"
_PDF_LOOKAHEAD = 4096


class WarcParseError(ValueError):
    """Raised when the bytes are not a WARC record carrying a PDF."""


def parse_pdf_payload(record_bytes: bytes) -> bytes:
    """Return the PDF payload of one WARC record, or raise WarcParseError."""
    record = _decoded_gzip_member(record_bytes) if record_bytes[:2] == _GZIP_MAGIC else record_bytes
    if not record.startswith(b"WARC/"):
        raise WarcParseError("these bytes do not start a WARC record")

    _, record_content = _split_header_block(record)
    http_headers, body = _split_header_block(record_content)
    body = _strip_record_separators(body)

    if http_headers.get("content-encoding") == "gzip":
        body = _gunzip_first_member(body)

    if not _looks_like_pdf(body):
        content_type = http_headers.get("content-type", "unknown")
        raise WarcParseError(f"the record payload is not a PDF (content-type: {content_type})")
    return body


def _decoded_gzip_member(record_bytes: bytes) -> bytes:
    try:
        return gzip.decompress(record_bytes)
    except OSError as error:
        raise WarcParseError(f"the range request did not return a decodable gzip member: {error}") from error


def _gunzip_first_member(body: bytes) -> bytes:
    """Decode one gzip member and ignore any trailing framing bytes."""
    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    try:
        return decompressor.decompress(body)
    except zlib.error as error:
        raise WarcParseError(f"the record body claims gzip but does not decode: {error}") from error


def _split_header_block(block: bytes) -> tuple[dict[str, str], bytes]:
    """Split a header block from its remainder; the first line (version line) is dropped."""
    end = block.find(_HEADER_END)
    if end < 0:
        raise WarcParseError("a header block is missing its terminating blank line")
    headers: dict[str, str] = {}
    for line in block[:end].split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return headers, block[end + len(_HEADER_END):]


def _strip_record_separators(body: bytes) -> bytes:
    while body.endswith(_HEADER_END):
        body = body[: -len(_HEADER_END)]
    return body


def _looks_like_pdf(body: bytes) -> bool:
    return body.lstrip()[: _PDF_LOOKAHEAD].startswith(_PDF_MAGIC)
