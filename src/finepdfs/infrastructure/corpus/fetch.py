"""Fetching a corpus: one range request per Crawl Record, resumable, failures tolerated."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from finepdfs.domain.crawl import CrawlQuery
from finepdfs.domain.document import content_digest
from finepdfs.domain.ports import CrawlSource
from finepdfs.infrastructure.corpus.manifest import (
    MANIFEST_NAME,
    CorpusEntry,
    CorpusManifest,
    pdf_path,
    relative_pdf_path,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FetchFailure:
    url: str
    reason: str


@dataclass(frozen=True, slots=True)
class FetchReport:
    records_seen: int = 0
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    failures: tuple[FetchFailure, ...] = ()

    def summary(self) -> str:
        return (
            f"records seen {self.records_seen}, fetched {self.fetched}, "
            f"already present {self.skipped}, failed {self.failed}"
        )


def fetch_corpus(
    source: CrawlSource,
    query: CrawlQuery,
    corpus_dir: Path,
    manifest_name: str = MANIFEST_NAME,
) -> FetchReport:
    """Fetch every record of a query that is not in the corpus yet."""
    manifest_path = corpus_dir / manifest_name
    manifest = CorpusManifest.load(manifest_path)
    if not manifest.entries:
        manifest.crawl_id = query.crawl_id
        manifest.domain = query.domain

    records = list(source.list_records(query))
    already_have = manifest.urls
    fetched = skipped = failed = 0
    failures: list[FetchFailure] = []

    for record in records:
        if record.url in already_have:
            skipped += 1
            continue
        try:
            payload = source.fetch(record)
        except Exception as error:  # one bad record must not end the run
            logger.warning("could not fetch %s: %s", record.url, error)
            failures.append(FetchFailure(url=record.url, reason=str(error)))
            failed += 1
            continue

        digest = content_digest(payload)
        target = pdf_path(corpus_dir, digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        manifest.add(
            CorpusEntry(
                url=record.url,
                digest=digest,
                file=relative_pdf_path(digest),
                bytes=len(payload),
                crawl_id=record.crawl_id,
                warc_filename=record.warc_filename,
                offset=record.offset,
                length=record.length,
            )
        )
        already_have.add(record.url)
        fetched += 1

    manifest.save(manifest_path)
    return FetchReport(
        records_seen=len(records),
        fetched=fetched,
        skipped=skipped,
        failed=failed,
        failures=tuple(failures),
    )
