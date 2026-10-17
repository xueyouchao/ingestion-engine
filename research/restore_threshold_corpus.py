"""Restore the pinned threshold corpus from Common Crawl; verify every PDF's bytes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from finepdfs.domain.crawl import CrawlRecord
from finepdfs.infrastructure.commoncrawl.source import CommonCrawlSource


def matches(payload: bytes, entry: dict) -> bool:
    return len(payload) == entry["bytes"] and hashlib.sha256(payload).hexdigest() == entry["digest"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    pinned = Path(__file__).with_name("per-language-thresholds-corpus-manifest.json")
    manifest = json.loads(pinned.read_text(encoding="utf-8"))
    source = CommonCrawlSource()
    for entry in manifest["entries"]:
        target = args.destination / entry["file"]
        if target.exists() and matches(target.read_bytes(), entry):
            print("verified", entry["url"])
            continue
        record = CrawlRecord(
            url=entry["url"], crawl_id=entry["crawl_id"], warc_filename=entry["warc_filename"],
            offset=entry["offset"], length=entry["length"],
        )
        payload = source.fetch(record)
        if not matches(payload, entry):
            raise ValueError(f"PDF size or SHA-256 mismatch: {entry['url']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        print("restored", entry["url"])
    args.destination.mkdir(parents=True, exist_ok=True)
    (args.destination / "manifest.json").write_bytes(pinned.read_bytes())


if __name__ == "__main__":
    main()
