"""Fetching a corpus: one range request per Crawl Record, resumable, failures tolerated."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
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
    """Fetch every record of a query that is not in the corpus yet.

    Resumable in the way that matters. A record counts as present only when its bytes are on disk at
    the size the manifest claims, so a half-written file is fetched again; the manifest is
    checkpointed after every stored record, so a crash keeps what was already fetched; and a record
    that cannot be fetched or stored is that record's problem rather than the run's.
    """
    manifest_path = corpus_dir / manifest_name
    manifest = CorpusManifest.load(manifest_path)
    if not manifest.entries:
        manifest.crawl_id = query.crawl_id
        manifest.domain = query.domain

    records = list(source.list_records(query))
    stored_by_url = {entry.url: entry for entry in manifest.entries}
    fetched = skipped = failed = 0
    failures: list[FetchFailure] = []

    for record in records:
        existing = stored_by_url.get(record.url)
        if existing is not None and _is_stored(corpus_dir, existing):
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
        try:
            _store(corpus_dir, digest, payload)
        except OSError as error:  # a full disk is one record's problem too
            logger.warning("could not store %s: %s", record.url, error)
            failures.append(FetchFailure(url=record.url, reason=str(error)))
            failed += 1
            continue

        entry = CorpusEntry(
            url=record.url,
            digest=digest,
            file=relative_pdf_path(digest),
            bytes=len(payload),
            crawl_id=record.crawl_id,
            warc_filename=record.warc_filename,
            offset=record.offset,
            length=record.length,
        )
        if existing is None:
            manifest.add(entry)
        else:
            manifest.replace(existing, entry)
        stored_by_url[record.url] = entry
        manifest.save(manifest_path)
        fetched += 1

    manifest.save(manifest_path)
    return FetchReport(
        records_seen=len(records),
        fetched=fetched,
        skipped=skipped,
        failed=failed,
        failures=tuple(failures),
    )


def _is_stored(corpus_dir: Path, entry: CorpusEntry) -> bool:
    """An entry counts as present only if its bytes are on disk at the size the manifest promises."""
    try:
        return pdf_path(corpus_dir, entry.digest).stat().st_size == entry.bytes
    except OSError:
        return False


def _store(corpus_dir: Path, digest: str, payload: bytes) -> None:
    """Write the bytes under their digest, atomically, so a crash cannot leave half a PDF behind."""
    target = pdf_path(corpus_dir, digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".pdf.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, target)
