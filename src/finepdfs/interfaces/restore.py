"""Replay one pinned manifest into a corpus directory: the single restore implementation.

`finepdfs restore-corpus` and `demo restore` are the same operation with different directories. The
manifest reading, the replay, the summary line and the failure lines live here once, so the two entry
points cannot drift into reporting a restore differently; the Fetch itself stays in the Fetch adapter
(ADR-0003). A pinned record that cannot be restored is a visible failure, never a silent gap.
"""

from __future__ import annotations

import sys
from pathlib import Path

from finepdfs.infrastructure.commoncrawl.source import CommonCrawlSource
from finepdfs.infrastructure.corpus.fetch import restore_corpus
from finepdfs.infrastructure.corpus.manifest import CorpusManifest


def restore_pinned_corpus(manifest_path: Path | str, corpus_dir: Path | str, *, source=None) -> int:
    """Fetch every record of one pinned manifest into ``corpus_dir``; 0, 1 for failures, 2 for no manifest."""
    pinned = CorpusManifest.load(manifest_path)
    if not pinned.entries:
        print(f"no pinned manifest at {manifest_path} (or it lists no entries)", file=sys.stderr)
        return 2
    source = source if source is not None else CommonCrawlSource()

    print(f"restoring {len(pinned.entries)} pinned PDFs from {pinned.crawl_id} into {corpus_dir}")
    report = restore_corpus(source, pinned, corpus_dir)

    print(report.summary())
    for failure in report.failures:
        print(f"  failed: {failure.url} ({failure.reason})")
    return 1 if report.failed else 0
