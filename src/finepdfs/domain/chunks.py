"""Deterministic chunks of accepted Page Texts, for lexical search.

A chunk is a citable slice of one Page Text: the search contract sends a reader back to a page, and
the offsets say exactly which characters were matched. Chunking is pure, so the same accepted pages
and the same ``CHUNKING_VERSION`` always produce the same chunk ids.

Rules (version ``chunking-v1``):

* every chunk lies inside one page, so its original 1-based page number is exact and no citation has
  to reason about a range of pages;
* a page is split at blank lines (paragraph boundaries) and paragraphs are grouped up to
  ``max_characters``; a paragraph longer than that is split at whitespace, and only a run of
  whitespace longer than the limit is cut without a boundary;
* every chunk is ``page.text[start_offset:end_offset]`` with whitespace trimmed at the edges, so the
  offsets are true and no chunk is empty.

Bumping the version changes every ``chunk_id`` without changing the text or the page numbers. That is
deliberate: a different chunking rule is different evidence, and the index must retract the old ids.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from finepdfs.domain.publishing import PageRecord

CHUNKING_VERSION = "chunking-v1"
DEFAULT_CHUNK_CHARACTERS = 1200

# A blank line: a newline, any whitespace and another newline. Paragraph text never includes it.
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


@dataclass(frozen=True, slots=True)
class DeclaredChunkSet:
    """The chunks the flow currently declares, and a fingerprint naming exactly that evidence.

    The fingerprint is what the search index records when it is built, so a reader can tell a current
    index from one that predates a replacement, a removal or a chunking-version bump (ADR-0008). Keeping
    the two fields together means a fingerprint can never be paired with a different chunk set.
    """

    chunks: tuple[Chunk, ...]
    fingerprint: str

    @classmethod
    def of(cls, chunks: Iterable[Chunk]) -> DeclaredChunkSet:
        items = tuple(chunks)
        return cls(chunks=items, fingerprint=chunk_set_fingerprint(items))


def chunk_set_fingerprint(chunks: Iterable[Chunk]) -> str:
    """A stable digest of exactly this evidence: every chunk identity and every chunk text.

    Identity alone would miss a replacement that kept the ids; text alone would miss a re-ordering.
    Both, in ``chunk_id`` order, move whenever declared evidence changes and never when it does not.
    """
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda item: item.chunk_id):
        digest.update(chunk.chunk_id.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(chunk.text.encode("utf-8"))
        digest.update(b"\x1e")
    return f"sha256:{digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class Chunk:
    """One accepted Page Text's slice, as the search index stores and cites it."""

    chunk_id: str
    doc_id: str
    source_url: str
    digest: str
    engine: str
    language: str
    chunking_version: str
    ordinal: int
    page_number: int
    start_offset: int
    end_offset: int
    text: str

    def as_json(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "source_url": self.source_url,
            "digest": self.digest,
            "engine": self.engine,
            "language": self.language,
            "chunking_version": self.chunking_version,
            "ordinal": self.ordinal,
            "page_number": self.page_number,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "text": self.text,
        }

    @classmethod
    def from_json(cls, payload: dict) -> Chunk:
        """Read one declared chunk. An unknown or missing field is a visible KeyError, not a guess."""
        return cls(
            chunk_id=payload["chunk_id"],
            doc_id=payload["doc_id"],
            source_url=payload["source_url"],
            digest=payload["digest"],
            engine=payload["engine"],
            language=payload["language"],
            chunking_version=payload["chunking_version"],
            ordinal=int(payload["ordinal"]),
            page_number=int(payload["page_number"]),
            start_offset=int(payload["start_offset"]),
            end_offset=int(payload["end_offset"]),
            text=payload["text"],
        )


def chunk_page_records(
    records: Sequence[PageRecord],
    *,
    language: str,
    max_characters: int = DEFAULT_CHUNK_CHARACTERS,
    version: str | None = None,
) -> tuple[Chunk, ...]:
    """Chunk accepted Page Texts in page order; nothing outside those pages and their text is used."""
    if max_characters < 1:
        raise ValueError(f"max_characters must be at least 1, not {max_characters}")
    chunking_version = version if version is not None else CHUNKING_VERSION

    chunks: list[Chunk] = []
    ordinal = 0
    for record in sorted(records, key=lambda item: item.page_number):
        for start, end in _chunk_spans(record.text, max_characters):
            text = record.text[start:end]
            chunks.append(
                Chunk(
                    chunk_id=chunk_identity(
                        doc_id=record.doc_id,
                        digest=record.digest,
                        page_number=record.page_number,
                        ordinal=ordinal,
                        start_offset=start,
                        end_offset=end,
                        version=chunking_version,
                    ),
                    doc_id=record.doc_id,
                    source_url=record.source_url,
                    digest=record.digest,
                    engine=record.engine,
                    language=language,
                    chunking_version=chunking_version,
                    ordinal=ordinal,
                    page_number=record.page_number,
                    start_offset=start,
                    end_offset=end,
                    text=text,
                )
            )
            ordinal += 1
    return tuple(chunks)


def chunk_identity(
    *,
    doc_id: str,
    digest: str,
    page_number: int,
    ordinal: int,
    start_offset: int,
    end_offset: int,
    version: str,
) -> str:
    """The stable id of one chunk: the source identity, the bytes, the page and the slice, versioned."""
    material = "\x1f".join(
        [
            version,
            doc_id,
            digest,
            str(page_number),
            str(ordinal),
            str(start_offset),
            str(end_offset),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _paragraph_spans(text: str) -> Iterator[tuple[int, int]]:
    position = 0
    for separator in _PARAGRAPH_BREAK.finditer(text):
        yield position, separator.start()
        position = separator.end()
    yield position, len(text)


def _chunk_spans(text: str, limit: int) -> Iterator[tuple[int, int]]:
    """The page-local spans to chunk, in order, trimmed so no chunk starts or ends in whitespace."""
    current: list[int] | None = None
    for start, end in _paragraph_spans(text):
        if not text[start:end].strip():
            continue
        if end - start > limit:
            if current is not None:
                trimmed = _trim(text, *current)
                if trimmed is not None:
                    yield trimmed
                current = None
            for span in _split_span(text, start, end, limit):
                trimmed = _trim(text, *span)
                if trimmed is not None:
                    yield trimmed
            continue
        if current is None:
            current = [start, end]
        elif end - current[0] <= limit:
            current[1] = end
        else:
            trimmed = _trim(text, *current)
            if trimmed is not None:
                yield trimmed
            current = [start, end]
    if current is not None:
        trimmed = _trim(text, *current)
        if trimmed is not None:
            yield trimmed


def _split_span(text: str, start: int, end: int, limit: int) -> Iterator[tuple[int, int]]:
    """Split one over-long paragraph at whitespace, keeping every piece within the limit."""
    cursor = start
    while end - cursor > limit:
        window_end = cursor + limit
        break_at = next(
            (index for index in range(window_end, cursor, -1) if text[index - 1].isspace()),
            cursor,
        )
        if break_at == cursor:
            yield cursor, window_end  # no whitespace to break at: the limit wins over the word
            cursor = window_end
        else:
            yield cursor, break_at
            cursor = break_at
    if cursor < end:
        yield cursor, end


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None
