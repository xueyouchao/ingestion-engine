"""Asking the Common Crawl index for PDFs, and reading what it answers."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from finepdfs.domain.crawl import CrawlQuery, CrawlRecord

INDEX_HOST = "https://index.commoncrawl.org"
COLL_INFO_URL = f"{INDEX_HOST}/collinfo.json"


def latest_crawl_id(coll_info: list[dict]) -> str:
    """The newest full crawl the index offers. The collection list is newest first."""
    for entry in coll_info:
        crawl_id = str(entry.get("id", ""))
        if crawl_id.startswith("CC-MAIN-"):
            return crawl_id
    raise ValueError("the collection list contains no CC-MAIN crawl")


def build_index_url(query: CrawlQuery) -> str:
    """The CDX API URL for a crawl query."""
    parameters = [
        ("url", query.domain),
        ("matchType", "domain"),
        ("filter", f"mime:{query.mime_type}"),
        ("filter", f"status:{query.status}"),
        ("output", "json"),
        ("limit", str(query.limit)),
    ]
    return f"{INDEX_HOST}/{query.crawl_id}-index?{urlencode(parameters)}"


def parse_index_response(response_text: str) -> list[CrawlRecord]:
    """Turn the index's JSON-lines answer into Crawl Records, skipping non-record lines."""
    records: list[CrawlRecord] = []
    for line in response_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        row = json.loads(line)
        if "filename" not in row or "offset" not in row:
            continue
        records.append(
            CrawlRecord(
                url=row["url"],
                crawl_id=crawl_id_from_warc_path(row["filename"]),
                warc_filename=row["filename"],
                offset=int(row["offset"]),
                length=int(row["length"]),
                mime_type=row.get("mime") or row.get("mime-detected") or "",
                status=int(row.get("status", 200)),
            )
        )
    return records


def crawl_id_from_warc_path(warc_filename: str) -> str:
    """The crawl id is the path segment after crawl-data, for example CC-MAIN-2026-39."""
    parts = warc_filename.split("/")
    if "crawl-data" in parts:
        index = parts.index("crawl-data")
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[0] if parts else ""
