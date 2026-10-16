"""Reading one Common Crawl WARC record out of a byte range.

A ranged read of a .warc.gz file returns a self-contained gzip member, so one record can be decoded
without the rest of the shard. Inside it sits a WARC header block, then an HTTP response whose body is
the PDF.

Everything here is strict on purpose. A host that ignores Range and answers with the whole shard, a
gzip member that stops in the middle, or a body shorter than the length its own headers declare all
produce a *different* document than the one that was asked for, silently, which is the worst failure a
corpus fetcher can have.
"""

from __future__ import annotations

import zlib

_HEADER_END = b"\r\n\r\n"
_GZIP_MAGIC = b"\x1f\x8b"
_PDF_MAGIC = b"%PDF"
_PDF_LOOKAHEAD = 4096
_FRAMING = b"\r\n"


class WarcParseError(ValueError):
    """Raised when the bytes are not one whole WARC record carrying a PDF."""


def parse_pdf_payload(record_bytes: bytes, expected_length: int | None = None) -> bytes:
    """Return the PDF payload of one WARC record, or raise WarcParseError.

    expected_length is how many bytes the range request asked for. When the answer is a different
    size, the bytes came from somewhere other than the region that was requested.
    """
    if expected_length is not None and len(record_bytes) != expected_length:
        raise WarcParseError(
            f"the range request asked for {expected_length} bytes and got {len(record_bytes)}"
        )

    record = _gunzip(record_bytes, "the range request") if record_bytes[:2] == _GZIP_MAGIC else record_bytes
    if not record.startswith(b"WARC/"):
        raise WarcParseError("these bytes do not start a WARC record")

    _, record_content = _split_header_block(record)
    http_headers, body = _split_header_block(record_content)
    body = _http_body(body, http_headers)

    if http_headers.get("content-encoding") == "gzip":
        body = _gunzip(body, "the record body")

    if not _looks_like_pdf(body):
        content_type = http_headers.get("content-type", "unknown")
        raise WarcParseError(f"the record payload is not a PDF (content-type: {content_type})")
    return body


def _gunzip(payload: bytes, what: str) -> bytes:
    """Decode exactly one gzip member, and refuse anything that is not exactly one whole member."""
    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    try:
        decoded = decompressor.decompress(payload)
    except zlib.error as error:
        raise WarcParseError(f"{what} is not a decodable gzip member: {error}") from error
    if not decompressor.eof:
        raise WarcParseError(f"{what} holds a gzip member that stops in the middle")
    if decompressor.unused_data.strip(_FRAMING):
        raise WarcParseError(f"{what} holds more than one gzip member; the range was too wide")
    return decoded


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
    return headers, block[end + len(_HEADER_END) :]


def _http_body(body: bytes, http_headers: dict[str, str]) -> bytes:
    """The body the response claims to carry: Content-Length bytes, or one framing separator less."""
    declared = http_headers.get("content-length")
    if declared is None:
        return body[: -len(_HEADER_END)] if body.endswith(_HEADER_END) else body
    try:
        length = int(declared)
    except ValueError as error:
        raise WarcParseError(f"the record declares an unreadable content-length: {declared}") from error
    if length <= 0 or length > len(body):
        raise WarcParseError(f"the record declares {declared} payload bytes and carries {len(body)}")
    return body[:length]


def _looks_like_pdf(body: bytes) -> bool:
    return body.lstrip()[: _PDF_LOOKAHEAD].startswith(_PDF_MAGIC)
