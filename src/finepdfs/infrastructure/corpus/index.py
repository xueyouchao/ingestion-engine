"""The corpus as the pipeline sees it: documents from the manifest, in a stable order."""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass

from finepdfs.infrastructure.corpus.manifest import MANIFEST_NAME, CorpusManifest


def document_key(url: str) -> str:
    """A short, stable, filesystem-safe name for one document.

    The first 16 hex digits of the SHA-256 of the URL: the same idea as the PDFs, which are named by
    their content digest, so a second run writes the same names.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    """One document in the corpus, with the key its published outputs are named after."""

    key: str
    url: str
    digest: str
    path: pathlib.Path


class CorpusIndex:
    """Reads the manifest and hands out documents; every path is absolute so it survives a move."""

    def __init__(self, corpus_dir: pathlib.Path) -> None:
        self._corpus_dir = pathlib.Path(corpus_dir)

    @property
    def manifest_path(self) -> pathlib.Path:
        return self._corpus_dir / MANIFEST_NAME

    def manifest(self) -> CorpusManifest:
        return CorpusManifest.load(self.manifest_path)

    def documents(self) -> tuple[CorpusDocument, ...]:
        """Every document, sorted by URL so two runs mount the same components."""
        manifest = self.manifest()
        documents = [
            CorpusDocument(
                key=document_key(entry.url),
                url=entry.url,
                digest=entry.digest,
                path=(self._corpus_dir / entry.file).resolve(),
            )
            for entry in manifest.entries
        ]
        return tuple(sorted(documents, key=lambda document: document.url))
