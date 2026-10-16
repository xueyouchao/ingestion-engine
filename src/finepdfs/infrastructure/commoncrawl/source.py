"""Fetching PDFs from Common Crawl: one range request per document."""

from __future__ import annotations

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
        return parse_index_response(body.decode("utf-8", "replace"))

    def fetch(self, record: CrawlRecord) -> bytes:
        start, end = record.byte_range
        raw = self._read(record.warc_url, byte_range=(start, end))
        return parse_pdf_payload(raw)

    def _read(self, url: str, byte_range: tuple[int, int] | None = None, tolerate_no_captures: bool = False) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        if byte_range is not None:
            start, end = byte_range
            request.add_header("Range", f"bytes={start}-{end}")

        last_error: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                body = error.read()
                if error.code == 404 and b"no captures" in body.lower():
                    # The index answers 404 with a JSON message when a domain has no matching captures.
                    logger.info("the index holds no captures for %s", url)
                    if tolerate_no_captures:
                        return body
                    raise CommonCrawlError(f"no captures for {url}") from error
                last_error = error
                logger.warning("attempt %d/%d failed for %s: %s", attempt, self._attempts, url, error)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
                logger.warning("attempt %d/%d failed for %s: %s", attempt, self._attempts, url, error)
            if attempt < self._attempts:
                time.sleep(self._pause * attempt)
        raise CommonCrawlError(f"could not read {url}: {last_error}") from last_error
