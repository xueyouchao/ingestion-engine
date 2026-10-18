"""Reading accepted evidence through the shared search contract, and refreshing its index.

The reader decides nothing: it queries the index of the chunks the flow declared. Reindexing reads the
current declared chunk set and makes the index exactly that set, so a replacement, fewer pages, an
exclusion or a removed Crawl Record stops being searchable without a second curation decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from finepdfs.domain.chunks import Chunk
from finepdfs.domain.ports import SearchIndex
from finepdfs.domain.search import (
    DEFAULT_TOP_K,
    MAX_TOP_K,
    IndexFailure,
    SearchFilters,
    SearchQuery,
    SearchResults,
)


@dataclass(frozen=True, slots=True)
class ReindexReport:
    """What one reindex did: how much was written, what was removed, and every failure."""

    documents: int
    chunks: int
    upserted: int
    removed: int
    failures: tuple[IndexFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


def reindex_search(index: SearchIndex, chunks: Sequence[Chunk]) -> ReindexReport:
    """Make the index hold exactly these declared chunks: upsert them, then delete everything else."""
    failures = tuple(index.upsert(chunks))
    # A failed chunk keeps its stored row (possibly older evidence) rather than losing its citation
    # entirely; the failure is reported instead of the deletion being silent.
    removed = index.retain(tuple(chunk.chunk_id for chunk in chunks))
    return ReindexReport(
        documents=len({chunk.doc_id for chunk in chunks}),
        chunks=len(chunks),
        upserted=len(chunks) - len(failures),
        removed=removed,
        failures=failures,
    )


class SearchAcceptedEvidence:
    """The one read interface the browser and the agent share; the backend is behind the port."""

    def __init__(self, index: SearchIndex) -> None:
        self._index = index

    def search(
        self,
        text: str,
        *,
        filters: SearchFilters | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> SearchResults:
        query = _query(text, filters, top_k)
        return SearchResults(query=query, hits=tuple(self._index.search(query)))


def search_pdfs(
    index: SearchIndex,
    query: str,
    filters: SearchFilters | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> SearchResults:
    """The agent-facing spelling of the same contract; ticket 08's tool calls this."""
    return SearchAcceptedEvidence(index).search(query, filters=filters, top_k=top_k)


def _query(text: str, filters: SearchFilters | None, top_k: int) -> SearchQuery:
    stripped = text.strip()
    if not stripped:
        raise ValueError("a search needs a non-empty query")
    if top_k < 1:
        raise ValueError(f"top_k must be at least 1, not {top_k}")
    # A bounded reader: an agent cannot ask for an unbounded result set, and a browser slider cannot
    # turn a search into a corpus dump.
    return SearchQuery(text=stripped, top_k=min(top_k, MAX_TOP_K), filters=filters or SearchFilters())
