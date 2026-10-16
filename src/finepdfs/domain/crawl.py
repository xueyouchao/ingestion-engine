"""Crawl Records: pointers into Common Crawl, not documents."""

from __future__ import annotations

from dataclasses import dataclass

DATA_HOST = "https://data.commoncrawl.org"


@dataclass(frozen=True, slots=True)
class CrawlRecord:
    """One row of the Common Crawl index: where a PDF sits inside one WARC file."""

    url: str
    crawl_id: str
    warc_filename: str
    offset: int
    length: int
    mime_type: str = "application/pdf"
    status: int = 200

    @property
    def warc_url(self) -> str:
        return f"{DATA_HOST}/{self.warc_filename}"

    @property
    def byte_range(self) -> tuple[int, int]:
        """Inclusive byte range of this record inside its WARC file."""
        return self.offset, self.offset + self.length - 1


@dataclass(frozen=True, slots=True)
class CrawlQuery:
    """What we ask the index for: a domain's PDFs from one crawl."""

    crawl_id: str
    domain: str
    mime_type: str = "application/pdf"
    status: int = 200
    limit: int = 50

    def __post_init__(self) -> None:
        if not self.crawl_id.strip():
            raise ValueError("a crawl query needs a crawl id, for example CC-MAIN-2026-39")
        if not self.domain.strip():
            raise ValueError("a crawl query needs a domain, for example noaa.gov")
        if self.limit < 1:
            raise ValueError("a crawl query needs a positive limit")
