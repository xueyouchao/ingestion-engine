"""Reading and writing the published corpus.

The layout is deliberately boring: one Markdown file per document, one JSON record per document, one
JSONL page export per published document, one JSON exclusion per document that was dropped or failed.
Every file is named by a key derived from the document URL, so a document never collides with another
one.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable, Sequence

from finepdfs.domain.dedup import DedupPlan
from finepdfs.domain.publishing import DocumentRecord, PageRecord
from finepdfs.infrastructure.corpus.index import document_key

DOCS = "docs"
RECORDS = "records"
PAGES = "pages"
EXCLUDED = "excluded"
DEDUPE = "dedupe"
KEPT_REPORT = "kept.jsonl"
DUPLICATES_REPORT = "duplicates.jsonl"


class PublishedCorpus:
    """The output directory, addressed by document key."""

    def __init__(self, root: pathlib.Path) -> None:
        self._root = pathlib.Path(root)

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def doc_path(self, key: str) -> pathlib.Path:
        return self._root / DOCS / f"{key}.md"

    def record_path(self, key: str) -> pathlib.Path:
        return self._root / RECORDS / f"{key}.json"

    def exclusion_path(self, key: str) -> pathlib.Path:
        return self._root / EXCLUDED / f"{key}.json"

    def pages_path(self, key: str) -> pathlib.Path:
        return self._root / PAGES / f"{key}.jsonl"

    def record_payload(self, record: DocumentRecord) -> str:
        return json.dumps(record.as_json(), indent=2) + "\n"

    def pages_payload(self, records: Sequence[PageRecord]) -> str:
        """One published document's accepted Page Texts, one JSON object per line, in page order."""
        return _jsonl(record.as_json() for record in sorted(records, key=lambda record: record.page_number))

    def exclusion_payload(self, url: str, reason: str, kind: str) -> str:
        return json.dumps({"url": url, "key": document_key(url), "kind": kind, "reason": reason}, indent=2) + "\n"

    def read_records(self) -> list[dict]:
        records = []
        for path in sorted((self._root / RECORDS).glob("*.json")):
            records.append(json.loads(path.read_text(encoding="utf-8")))
        return records

    def read_exclusions(self) -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self._root / EXCLUDED).glob("*.json"))
        ]

    def dedup_report_path(self, name: str) -> pathlib.Path:
        return self._root / DEDUPE / name

    def kept_payload(self, plan: DedupPlan) -> str:
        """The URLs this plan keeps, one JSON object per line."""
        return _jsonl({"url": url} for url in plan.keepers)

    def duplicates_payload(self, plan: DedupPlan) -> str:
        """Every dropped duplicate, with what it duplicates and how alike they are."""
        return _jsonl(
            {
                "url": decision.url,
                "kind": decision.kind,
                "duplicate_of": decision.duplicate_of,
                "similarity": decision.similarity,
            }
            for decision in plan.duplicates
        )


def _jsonl(rows: Iterable[dict]) -> str:
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
