"""Reading and writing the published corpus.

The layout is deliberately boring: one Markdown file per document, one JSON record per document, one
JSON exclusion per document that was dropped or failed. Every file is named by a key derived from the
document URL, so a document never collides with another one.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Iterable

from finepdfs.domain.dedup import DedupPlan, PublishedDocumentView
from finepdfs.domain.publishing import DocumentRecord
from finepdfs.infrastructure.corpus.index import document_key

DOCS = "docs"
RECORDS = "records"
EXCLUDED = "excluded"
DEDUPE = "dedupe"


@dataclass(frozen=True, slots=True)
class DedupReport:
    """What a dedup pass did to the published corpus."""

    kept: int
    duplicates: int
    removed_documents: int
    report_path: pathlib.Path


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

    def record_payload(self, record: DocumentRecord) -> str:
        return json.dumps(record.as_json(), indent=2) + "\n"

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

    def read_documents(self) -> list[PublishedDocumentView]:
        """Published documents with their text, for the corpus-wide dedup pass."""
        documents = []
        for record in self.read_records():
            key = document_key(record["url"])
            doc_path = self.doc_path(key)
            if not doc_path.exists():
                continue
            documents.append(PublishedDocumentView(url=record["url"], digest=record["digest"], text=doc_path.read_text(encoding="utf-8")))
        return documents

    def apply_dedup_plan(self, plan: DedupPlan) -> DedupReport:
        """Mark duplicates in their records, remove their documents, and write the report."""
        decisions = {decision.url: decision for decision in plan.duplicates}
        removed = 0
        for record in self.read_records():
            decision = decisions.get(record["url"])
            key = document_key(record["url"])
            if decision is None:
                record["dedup_status"], record["duplicate_of"] = "unique", ""
            else:
                record["dedup_status"], record["duplicate_of"] = decision.kind, decision.duplicate_of
                doc_path = self.doc_path(key)
                if doc_path.exists():
                    doc_path.unlink()
                    removed += 1
            self.record_path(key).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

        report_dir = self._root / DEDUPE
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "duplicates.jsonl"
        _write_jsonl(report_path, (record.__dict__ for record in plan.duplicates))
        _write_jsonl(
            report_dir / "kept.jsonl",
            ({"url": url} for url in plan.keepers),
        )
        return DedupReport(
            kept=len(plan.keepers),
            duplicates=len(plan.duplicates),
            removed_documents=removed,
            report_path=report_path,
        )


def _write_jsonl(path: pathlib.Path, rows: Iterable[dict]) -> None:
    body = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    path.write_text(body, encoding="utf-8")
