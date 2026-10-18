"""Reading and writing the published corpus.

The layout is deliberately boring: one Markdown file per document, one JSON record per document, one
JSONL page export per published document, one JSONL chunk export per published document, one JSON
exclusion per document that was dropped or failed. Every file is named by a key derived from the
document URL, so a document never collides with another one.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable, Sequence

from finepdfs.domain.chunks import Chunk, DeclaredChunkSet
from finepdfs.domain.dedup import DedupPlan
from finepdfs.domain.publishing import DocumentRecord, PageRecord
from finepdfs.domain.source_identity import require_source_identity
from finepdfs.infrastructure.corpus.index import document_key

DOCS = "docs"
RECORDS = "records"
PAGES = "pages"
CHUNKS = "chunks"
EXCLUDED = "excluded"
DEDUPE = "dedupe"
KEPT_REPORT = "kept.jsonl"
DUPLICATES_REPORT = "duplicates.jsonl"
RUN_SUMMARY = "run-summary.json"


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

    def chunks_path(self, key: str) -> pathlib.Path:
        return self._root / CHUNKS / f"{key}.jsonl"

    def record_payload(self, record: DocumentRecord) -> str:
        return json.dumps(record.as_json(), indent=2) + "\n"

    def pages_payload(self, records: Sequence[PageRecord]) -> str:
        """One published document's accepted Page Texts, one JSON object per line, in page order."""
        return _jsonl(record.as_json() for record in sorted(records, key=lambda record: record.page_number))

    def chunks_payload(self, chunks: Sequence[Chunk]) -> str:
        """One published document's chunks, one JSON object per line, in ordinal order."""
        return _jsonl(chunk.as_json() for chunk in sorted(chunks, key=lambda chunk: chunk.ordinal))

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

    def read_declared_chunks(self) -> DeclaredChunkSet:
        """Every currently declared chunk, plus the fingerprint naming exactly that evidence.

        An excluded, duplicated or removed Document declares no chunk file, so target state has
        already retracted it before this reader runs: there is no second curation decision here. The
        fingerprint is what a search index records when it is rebuilt, so a reader can tell whether
        the rows it would return still describe this set.
        """
        chunks: list[Chunk] = []
        for path in sorted((self._root / CHUNKS).glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    chunks.append(Chunk.from_json(json.loads(line)))
        return DeclaredChunkSet.of(chunks)

    def dedup_report_path(self, name: str) -> pathlib.Path:
        return self._root / DEDUPE / name

    def summary_path(self) -> pathlib.Path:
        """The run's measured counters, declared by the flow and read by people and browsers."""
        return self._root / RUN_SUMMARY

    def summary_payload(self, summary: dict) -> str:
        return json.dumps(summary, indent=2) + "\n"

    def read_summary(self) -> dict | None:
        path = self.summary_path()
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

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


class DiskPageReader:
    """``DeclaredPageReader`` over the output directory: the same files the flow declared.

    It reads only ``records/``, ``excluded/`` and ``pages/``. Markdown, the run summary and every other
    output are not evidence sources here, so a citation cannot be reconstructed from a projection.
    """

    def __init__(self, root: pathlib.Path | str) -> None:
        self._root = pathlib.Path(root)

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def has_output(self) -> bool:
        return self._root.is_dir()

    def published_digest(self, doc_id: str) -> str | None:
        path = self._path(RECORDS, doc_id, ".json")
        if not path.is_file():
            return None
        payload = _read_object(path)
        digest = payload.get("digest")
        if not isinstance(digest, str) or not digest:
            raise ValueError(f"{path} does not name a published digest")
        return digest

    def exclusion(self, doc_id: str) -> tuple[str, str] | None:
        path = self._path(EXCLUDED, doc_id, ".json")
        if not path.is_file():
            return None
        payload = _read_object(path)
        kind = payload.get("kind")
        reason = payload.get("reason")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"{path} does not name an exclusion kind")
        return kind, reason if isinstance(reason, str) else ""

    def accepted_pages(self, doc_id: str) -> tuple[PageRecord, ...] | None:
        path = self._path(PAGES, doc_id, ".jsonl")
        if not path.is_file():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise ValueError(f"{path} could not be read: {error}") from error
        pages = []
        for line in lines:
            if not line.strip():
                continue
            payload = _parse_object(line, path)
            try:
                pages.append(
                    PageRecord(
                        doc_id=payload["doc_id"],
                        source_url=payload["source_url"],
                        digest=payload["digest"],
                        page_number=int(payload["page_number"]),
                        text=payload["text"],
                        engine=payload["engine"],
                    )
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{path} holds an invalid page record: {error}") from error
        return tuple(sorted(pages, key=lambda page: page.page_number))

    def _path(self, directory: str, doc_id: str, suffix: str) -> pathlib.Path:
        return self._root / directory / f"{require_source_identity(doc_id)}{suffix}"


def _read_object(path: pathlib.Path) -> dict:
    try:
        return _parse_object(path.read_text(encoding="utf-8"), path)
    except OSError as error:
        raise ValueError(f"{path} could not be read: {error}") from error


def _parse_object(text: str, path: pathlib.Path) -> dict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not valid JSON: expected an object")
    return payload
