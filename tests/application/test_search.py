"""The search use case: one validated, bounded read over whatever index satisfies the port.

The backend is not under test here -- a hand-written fake implements the same ``SearchIndex`` port --
so what stays pinned is the contract a browser or an agent gets: a non-empty query, a bounded top_k,
the filters passed through unchanged, and a reindex report that never hides a per-chunk failure.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from finepdfs.application.search import ReindexReport, SearchAcceptedEvidence, reindex_search, search_pdfs
from finepdfs.domain.chunks import DeclaredChunkSet, chunk_page_records
from finepdfs.domain.publishing import PageRecord
from finepdfs.domain.search import MAX_TOP_K, IndexFailure, IndexState, SearchFilters


class FakeIndex:
    """A small hand-written index: it records what it was asked and answers the contract.

    ``state`` reports the fingerprint it was last built from, so a reader can tell a current index
    from a stale one without reaching into a backend.
    """

    def __init__(
        self,
        failures: tuple[IndexFailure, ...] = (),
        removed: int = 0,
        *,
        fingerprint: str | None = None,
        chunks: int = 0,
    ) -> None:
        self.queries = []
        self.upserted: list = []
        self.retained: tuple[str, ...] | None = None
        self.recorded: list[str] = []
        self._failures = failures
        self._removed = removed
        self._state = IndexState(fingerprint=fingerprint, chunks=chunks)

    def upsert(self, chunks):
        self.upserted.extend(chunks)
        return self._failures

    def retain(self, chunk_ids):
        self.retained = tuple(chunk_ids)
        return self._removed

    def record_freshness(self, fingerprint):
        self.recorded.append(fingerprint)

    def state(self):
        return self._state

    def search(self, query):
        self.queries.append(query)
        return ()


def _chunks(count: int = 2):
    records = tuple(
        PageRecord(
            doc_id=f"{index:016x}",
            source_url=f"https://example.org/{index}.pdf",
            digest=str(index) * 64,
            page_number=1,
            text=f"Accepted page {index}",
            engine="pymupdf@native",
        )
        for index in range(count)
    )
    return tuple(chunk for record in records for chunk in chunk_page_records((record,), language="eng"))


NO_DECLARED = DeclaredChunkSet.of(())


def _fresh_index(*, chunks: int = 1, **kwargs) -> FakeIndex:
    """An index whose recorded fingerprint matches the declared set the reader is given."""
    return FakeIndex(fingerprint=NO_DECLARED.fingerprint, chunks=chunks, **kwargs)


def test_an_empty_query_is_rejected_before_any_backend_call() -> None:
    index = _fresh_index()

    with pytest.raises(ValueError, match="non-empty query"):
        SearchAcceptedEvidence(index, lambda: NO_DECLARED).search("   \n\t ")

    assert index.queries == []


def test_top_k_is_bounded_to_what_the_contract_promises() -> None:
    index = _fresh_index()
    reader = SearchAcceptedEvidence(index, lambda: NO_DECLARED)

    reader.search("alpine", top_k=MAX_TOP_K * 10)
    reader.search("alpine", top_k=3)

    assert [query.top_k for query in index.queries] == [MAX_TOP_K, 3]
    with pytest.raises(ValueError, match="top_k"):
        reader.search("alpine", top_k=0)


def test_filters_and_query_text_reach_the_backend_unchanged() -> None:
    index = _fresh_index()
    filters = SearchFilters(language="eng", engine="docling@cli", doc_id="abc")

    results = SearchAcceptedEvidence(index, lambda: NO_DECLARED).search(
        "  alpine meteorology  ", filters=filters, top_k=5
    )

    query = index.queries[0]
    assert query.text == "alpine meteorology"
    assert query.filters == filters
    assert query.top_k == 5
    assert results.query == query
    assert results.total == 0


def test_the_agent_facing_function_is_the_same_contract() -> None:
    index = _fresh_index()
    filters = SearchFilters(doc_id="abc")

    result = search_pdfs(index, lambda: NO_DECLARED, "alpine", filters, top_k=2)

    assert result.query.text == "alpine"
    assert result.query.filters == filters
    assert result.query.top_k == 2


def test_a_reindex_reports_what_stored_what_failed_and_what_was_retracted() -> None:
    chunks = _chunks(2)
    failure = IndexFailure(chunk_id=chunks[1].chunk_id, reason="disk on fire")
    index = FakeIndex(failures=(failure,), removed=3)

    report = reindex_search(index, DeclaredChunkSet.of(chunks))

    assert report == ReindexReport(
        documents=2,
        chunks=2,
        upserted=1,
        removed=3,
        failures=(failure,),
    )
    assert not report.ok
    assert index.recorded == [], "a partial rebuild may not claim to mirror the declared evidence"
    assert index.retained == tuple(chunk.chunk_id for chunk in chunks), (
        "a failed chunk keeps its declared id so retain does not silently drop older evidence"
    )


def test_a_clean_reindex_is_ok_and_records_the_declared_fingerprint() -> None:
    declared = DeclaredChunkSet.of(_chunks(1))
    index = FakeIndex()

    report = reindex_search(index, declared)

    assert report.ok
    assert report.upserted == report.chunks == 1
    assert replace(report, removed=0).failures == ()
    assert index.recorded == [declared.fingerprint], "a clean rebuild names the evidence it mirrors"


def test_a_stale_index_is_unavailable_instead_of_answering_from_older_evidence() -> None:
    index = FakeIndex(fingerprint="sha256:the-one-before-the-replacement", chunks=4)

    results = SearchAcceptedEvidence(index, lambda: NO_DECLARED).search("alpine")

    assert results.available is False
    assert results.hits == ()
    assert "stale" in results.reason
    assert "index-search" in results.reason
    assert index.queries == [], "a stale index is never queried"


def test_an_empty_index_is_unavailable_not_an_empty_result_set() -> None:
    index = FakeIndex(fingerprint=NO_DECLARED.fingerprint, chunks=0)

    results = SearchAcceptedEvidence(index, lambda: NO_DECLARED).search("alpine")

    assert results.available is False
    assert "no chunks" in results.reason
    assert index.queries == []
