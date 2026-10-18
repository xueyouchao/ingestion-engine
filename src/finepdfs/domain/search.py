"""The search contract: one query, one hit shape, and no backend name in either.

The browser and the agent read accepted evidence through this shape and nothing else, so replacing the
backend cannot change what a citation means. A hit is one Chunk -- the source identity, source URL, PDF
digest, original 1-based page number, the chunk it came out of and its text -- plus the highlighted
snippet and the lexical score the query added. Composing the Chunk is what keeps a citation field from
being written twice.

Scores are BM25 with the score's sign flipped so that a larger number is a better match, because that
is the direction a reader and a ranked list expect. Only the order is guaranteed; the magnitude belongs
to the backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from finepdfs.domain.chunks import Chunk

DEFAULT_TOP_K = 10
MAX_TOP_K = 100


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Metadata filters, all optional; unset means "any"."""

    language: str | None = None
    engine: str | None = None
    doc_id: str | None = None


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """What a reader asked for."""

    text: str
    top_k: int = DEFAULT_TOP_K
    filters: SearchFilters = field(default_factory=SearchFilters)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One accepted chunk, cited: the chunk itself plus what the query ranked it by."""

    chunk: Chunk
    snippet: str
    score: float

    def as_json(self) -> dict:
        return {**self.chunk.as_json(), "snippet": self.snippet, "score": self.score}


class SearchUnavailable(Exception):
    """The index cannot answer right now: absent, empty, schema-less or built from other evidence.

    It is a state of derived state, not a failure of the query. A reader answers it with an explicit
    unavailable result naming what to run, never with hits from an index it cannot vouch for.
    """


@dataclass(frozen=True, slots=True)
class IndexState:
    """What the index says about itself: the declared-evidence fingerprint it mirrors and its size."""

    fingerprint: str | None
    chunks: int


@dataclass(frozen=True, slots=True)
class SearchResults:
    """The hits for one query, in score order -- or the reason there are none to trust.

    ``available`` is false when the index is absent, empty, schema-less or stale. Then ``hits`` is
    empty and ``reason`` says what state the reader is in and what to run: an unavailable answer is
    never dressed up as "no matches".
    """

    query: SearchQuery
    hits: tuple[SearchHit, ...]
    available: bool = True
    reason: str = ""

    @classmethod
    def unavailable(cls, query: SearchQuery, reason: str) -> SearchResults:
        return cls(query=query, hits=(), available=False, reason=reason)

    @property
    def total(self) -> int:
        return len(self.hits)

    def as_json(self) -> dict:
        return {
            "available": self.available,
            "reason": self.reason,
            "query": self.query.text,
            "filters": {
                "language": self.query.filters.language,
                "engine": self.query.filters.engine,
                "doc_id": self.query.filters.doc_id,
            },
            "total": self.total,
            "hits": [hit.as_json() for hit in self.hits],
        }


@dataclass(frozen=True, slots=True)
class IndexFailure:
    """One chunk the index could not store, with why. Never folded into a count alone."""

    chunk_id: str
    reason: str

    def as_json(self) -> dict:
        return {"chunk_id": self.chunk_id, "reason": self.reason}
