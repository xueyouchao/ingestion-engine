"""The corpus manifest: what was fetched, from where, and under which digest."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_NAME = "manifest.json"
PDF_DIRECTORY = "pdfs"


@dataclass(frozen=True, slots=True)
class CorpusEntry:
    """One fetched document."""

    url: str
    digest: str
    file: str
    bytes: int
    crawl_id: str
    warc_filename: str
    offset: int
    length: int


@dataclass
class CorpusManifest:
    """The corpus index, written next to the PDFs."""

    crawl_id: str = ""
    domain: str = ""
    entries: list[CorpusEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "CorpusManifest":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            crawl_id=raw.get("crawl_id", ""),
            domain=raw.get("domain", ""),
            entries=[CorpusEntry(**entry) for entry in raw.get("entries", [])],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"crawl_id": self.crawl_id, "domain": self.domain, "entries": [asdict(e) for e in self.entries]}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @property
    def urls(self) -> set[str]:
        return {entry.url for entry in self.entries}

    @property
    def digests(self) -> set[str]:
        return {entry.digest for entry in self.entries}

    def add(self, entry: CorpusEntry) -> None:
        self.entries.append(entry)


def pdf_path(corpus_dir: Path, digest: str) -> Path:
    return corpus_dir / PDF_DIRECTORY / f"{digest}.pdf"


def relative_pdf_path(digest: str) -> str:
    return f"{PDF_DIRECTORY}/{digest}.pdf"
