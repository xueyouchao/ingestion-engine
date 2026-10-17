"""Fetching PDFs from Common Crawl: one range request per document."""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.request

from finepdfs.domain.crawl import CrawlRecord
from finepdfs.domain.ports import CrawlSource
from finepdfs.infrastructure.commoncrawl.cdx import (
    COLL_INFO_URL,
    build_index_url,
    latest_crawl_id,
    parse_index_response,
)
from finepdfs.infrastructure.commoncrawl.warc import parse_pdf_payload

logger = logging.getLogger(__name__)

USER_AGENT = "finepdfs-cocoindex/0.1 (research; contact: local)"
RETRYABLE_STATUSES = frozenset({408, 425, 429})


class CommonCrawlError(RuntimeError):
    """Raised when the index or the data host cannot be read."""


class CommonCrawlSource(CrawlSource):
    """The live Common Crawl index and data host."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        attempts: int = 4,
        pause_seconds: float = 3.0,
        opener=None,
    ) -> None:
        self._timeout = timeout_seconds
        self._attempts = attempts
        self._pause = pause_seconds
        self._opener = opener or urllib.request.urlopen

    def latest_crawl_id(self) -> str:
        """The newest crawl, so a query does not have to name one."""
        return latest_crawl_id(json.loads(self._read(COLL_INFO_URL).decode("utf-8")))

    def list_records(self, query) -> list[CrawlRecord]:
        body = self._read(build_index_url(query), tolerate_no_captures=True)
        text = body.decode("utf-8", "replace")
        records = parse_index_response(text)
        if not records and not _mentions_no_captures(text):
            raise CommonCrawlError(
                f"the index answered without a single usable record for {query.domain}: {_first_line(text)}"
            )
        return records

    def fetch(self, record: CrawlRecord) -> bytes:
        start, end = record.byte_range
        raw = self._read(record.warc_url, byte_range=(start, end))
        return parse_pdf_payload(raw, expected_length=end - start + 1)

    def _read(self, url: str, byte_range: tuple[int, int] | None = None, tolerate_no_captures: bool = False) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        if byte_range is not None:
            start, end = byte_range
            request.add_header("Range", f"bytes={start}-{end}")

        last_error: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                try:
                    response = self._opener(request, timeout=self._timeout)
                except urllib.error.HTTPError as error:
                    with error:
                        body = error.read()
                    if error.code == 404 and _mentions_no_captures(body):
                        # The index answers 404 with a JSON message when a domain has no matching captures.
                        logger.info("the index holds no captures for %s", url)
                        if tolerate_no_captures:
                            return body
                        raise CommonCrawlError(f"no captures for {url}") from error
                    last_error = error
                    if not _is_worth_retrying(error.code):
                        logger.warning("giving up on %s: HTTP %s", url, error.code)
                        break
                    logger.warning("attempt %d/%d failed for %s: %s", attempt, self._attempts, url, error)
                else:
                    with response:
                        body = response.read()
                        if byte_range is not None:
                            _check_range_response(response, byte_range, len(body))
                        return body
            except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead) as error:
                # IncompleteRead derives from HTTPException, not OSError, so it needs naming: a body cut
                # mid-transfer is a transport failure, including while reading an HTTPError's body.
                last_error = error
                logger.warning("attempt %d/%d failed for %s: %s", attempt, self._attempts, url, error)
            if attempt < self._attempts:
                time.sleep(self._pause * attempt)
        raise CommonCrawlError(f"could not read {url}: {last_error}") from last_error


def _is_worth_retrying(status: int) -> bool:
    """A 400 or a 404 will still be a 400 or a 404 in three seconds. A 503 may not be."""
    return status >= 500 or status in RETRYABLE_STATUSES


def _check_range_response(response, byte_range: tuple[int, int], body_length: int) -> None:
    """Make sure the answer really is the range that was asked for."""
    start, end = byte_range
    status = getattr(response, "status", None)
    headers = getattr(response, "headers", None)
    content_range = headers.get("Content-Range") if headers is not None else None

    if content_range is None:
        if status == 200:
            raise CommonCrawlError(
                f"the data host ignored the Range request for bytes {start}-{end} "
                "and answered with the whole file"
            )
        return

    declared = _parse_content_range(content_range)
    if declared is None:
        raise CommonCrawlError(f"the data host answered with an unreadable Content-Range: {content_range}")
    declared_start, declared_end = declared
    if declared_start != start or body_length != declared_end - declared_start + 1:
        raise CommonCrawlError(
            f"asked for bytes {start}-{end} but the Content-Range says {content_range} "
            f"for a body of {body_length} bytes"
        )


def _parse_content_range(value: str) -> tuple[int, int] | None:
    """Read "bytes 100-150/19355" as (100, 150)."""
    _, _, span = value.partition(" ")
    first, separator, rest = span.partition("-")
    last = rest.partition("/")[0]
    if not separator:
        return None
    try:
        return int(first), int(last)
    except ValueError:
        return None


def _mentions_no_captures(body: bytes | str) -> bool:
    text = body if isinstance(body, str) else body.decode("latin-1", "replace")
    return "no captures" in text.lower()


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:200]
