"""The agent's two tools and its bounded, visible loop.

The tools read the same accepted evidence as the browser: ``search_pdfs`` over the shared search
contract and ``read_pdf_page`` over the declared page exports. The loop is exercised with a fake chat
model, so what stays pinned is the protocol an OpenAI-compatible endpoint sees: the tool schemas, the
tool result round-trip, the hard tool-call budget, and citations that can only name evidence a tool
actually returned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finepdfs.application.agent import (
    MAX_TOOL_CALLS,
    AcceptedEvidenceTools,
    run_agent,
)
from finepdfs.application.search import reindex_search
from finepdfs.domain.chunks import DeclaredChunkSet, chunk_page_records
from finepdfs.domain.publishing import PageRecord
from finepdfs.domain.search import IndexState
from finepdfs.infrastructure.corpus.index import document_key
from finepdfs.infrastructure.publishing.disk import DiskPageReader
from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex

URL = "https://a.example/one.pdf"
DUPLICATE_URL = "https://a.example/duplicate.pdf"
DIGEST = "1" * 64


# The tools refuse to answer from an index that does not mirror the declared evidence, so every fake
# index reports the fingerprint of the declared set the tools were given.
NO_DECLARED = DeclaredChunkSet.of(())


class RecordingIndex:
    """A hand-written SearchIndex: it records the query and returns one fixed hit."""

    def __init__(self) -> None:
        self.queries = []

    def upsert(self, chunks):  # pragma: no cover - the tool never writes
        return ()

    def retain(self, chunk_ids):  # pragma: no cover - the tool never writes
        return 0

    def record_freshness(self, fingerprint):  # pragma: no cover - the tool never writes
        pass

    def state(self):
        return IndexState(fingerprint=NO_DECLARED.fingerprint, chunks=1)

    def search(self, query):
        self.queries.append(query)
        return ()


class FakeChatModel:
    """An OpenAI-compatible model whose answers are scripted; requests are kept for inspection."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []

    def complete(self, messages, tools=()):
        self.requests.append(
            {"messages": [dict(message) for message in messages], "tools": list(tools)}
        )
        if not self._responses:
            raise AssertionError("the loop called the model more times than the script allowed")
        return self._responses.pop(0)


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _accepted_output(tmp_path: Path) -> tuple[Path, str, str]:
    out = tmp_path / "out"
    key = document_key(URL)
    page = PageRecord(
        doc_id=key,
        source_url=URL,
        digest=DIGEST,
        page_number=1,
        text="alpine meteorology observation on the accepted page",
        engine="pymupdf@1.28.2",
    )
    pages = (page, PageRecord(**{**page.as_json(), "page_number": 2, "text": "a storm flight identifier"}))
    (out / "records").mkdir(parents=True)
    (out / "pages").mkdir()
    (out / "records" / f"{key}.json").write_text(
        json.dumps(
            {
                "url": URL,
                "digest": DIGEST,
                "engine": "pymupdf@1.28.2",
                "routing_decision": "text-layer",
                "routing_reason": "healthy text layer",
                "language": "eng",
                "page_count": 2,
                "accepted_pages": 2,
                "dropped_pages": 0,
                "character_count": 85,
            }
        ),
        encoding="utf-8",
    )
    (out / "pages" / f"{key}.jsonl").write_text(
        "".join(json.dumps(record.as_json()) + "\n" for record in pages), encoding="utf-8"
    )
    duplicate = document_key(DUPLICATE_URL)
    (out / "excluded").mkdir()
    (out / "excluded" / f"{duplicate}.json").write_text(
        json.dumps(
            {
                "url": DUPLICATE_URL,
                "key": duplicate,
                "kind": "exact-duplicate",
                "reason": f"identical bytes to {URL}",
            }
        ),
        encoding="utf-8",
    )
    return out, key, duplicate


def _tools(out: Path, *, top_k: int = 3) -> AcceptedEvidenceTools:
    pages = tuple(
        PageRecord(
            doc_id=document_key(URL),
            source_url=URL,
            digest=DIGEST,
            page_number=ordinal,
            text=text,
            engine="pymupdf@1.28.2",
        )
        for ordinal, text in (
            (1, "alpine meteorology observation on the accepted page"),
            (2, "a storm flight identifier"),
        )
    )
    declared = DeclaredChunkSet.of(
        chunk for page in pages for chunk in chunk_page_records((page,), language="eng")
    )
    index = SqliteFtsSearchIndex(out / "search.db")
    reindex_search(index, declared)
    return AcceptedEvidenceTools(
        index, DiskPageReader(out), lambda: declared, default_top_k=top_k, max_top_k=5
    )


def test_search_tool_passes_query_filters_and_top_k_through_the_shared_contract() -> None:
    index = RecordingIndex()
    tools = AcceptedEvidenceTools(
        index, DiskPageReader(Path("/nowhere")), lambda: NO_DECLARED, default_top_k=4, max_top_k=6
    )

    result = tools.call(
        "search_pdfs",
        {"query": "  alpine storm  ", "filters": {"language": "eng", "doc_id": "a" * 16}, "top_k": 2},
    )

    query = index.queries[0]
    assert query.text == "alpine storm"
    assert query.filters.language == "eng"
    assert query.filters.doc_id == "a" * 16
    assert query.filters.engine is None
    assert query.top_k == 2
    assert result["total"] == 0
    assert result["hits"] == []


def test_a_requested_top_k_is_bounded_by_the_tool(tmp_path) -> None:
    out, _key, _duplicate = _accepted_output(tmp_path)
    declared = DeclaredChunkSet.of(
        chunk
        for page in (
            PageRecord(document_key(URL), URL, DIGEST, 1, "alpine alpine alpine", "pymupdf@1"),
        )
        for chunk in chunk_page_records((page,), language="eng")
    )
    reindex_search(SqliteFtsSearchIndex(out / "search.db"), declared)
    tools = AcceptedEvidenceTools(
        SqliteFtsSearchIndex(out / "search.db"), DiskPageReader(out), lambda: declared, max_top_k=5
    )

    result = tools.call("search_pdfs", {"query": "alpine", "top_k": 50})

    assert len(result["hits"]) <= 5
    assert result["top_k"] == 5


def test_the_search_tool_returns_each_citable_field(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)

    result = tools.call("search_pdfs", {"query": "alpine", "top_k": 3})

    assert result["total"] == 1
    hit = result["hits"][0]
    assert hit["doc_id"] == key
    assert hit["chunk_id"]
    assert hit["source_url"] == URL
    assert hit["digest"] == DIGEST
    assert hit["page_number"] == 1
    assert "alpine" in hit["text"]
    assert "<mark>" in hit["snippet"]
    assert hit["score"] > 0


def test_the_read_tool_reports_absent_and_excluded_pages_as_values(tmp_path) -> None:
    out, key, duplicate = _accepted_output(tmp_path)
    tools = _tools(out)

    absent = tools.call("read_pdf_page", {"doc_id": "0" * 16, "page_number": 1})
    excluded = tools.call("read_pdf_page", {"doc_id": duplicate, "page_number": 1})
    dropped = tools.call("read_pdf_page", {"doc_id": key, "page_number": 99})

    assert absent["status"] == "not-found"
    assert excluded["status"] == "unsupported"
    assert "exact-duplicate" in excluded["reason"]
    assert dropped["status"] == "not-found"
    assert dropped["accepted_pages"] == [1, 2]


def test_the_read_tool_refuses_stale_evidence_after_a_replacement(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    record_path = out / "records" / f"{key}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["digest"] = "2" * 64
    record_path.write_text(json.dumps(record), encoding="utf-8")

    result = tools.call("read_pdf_page", {"doc_id": key, "page_number": 1})

    assert result["status"] == "unsupported"
    assert "stale" in result["reason"]
    assert result["status"] != "accepted"


def test_the_read_tool_refuses_stale_evidence_after_a_removal(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    (out / "records" / f"{key}.json").unlink()

    result = tools.call("read_pdf_page", {"doc_id": key, "page_number": 1})

    assert result["status"] == "unsupported"
    assert "no current published record" in result["reason"]


def test_the_tools_expose_openai_function_schemas_without_a_backend_name() -> None:
    tools = AcceptedEvidenceTools(
        RecordingIndex(), DiskPageReader(Path("/nowhere")), lambda: NO_DECLARED
    )

    schemas = {schema["function"]["name"]: schema for schema in tools.schemas}

    assert set(schemas) == {"search_pdfs", "read_pdf_page"}
    assert schemas["search_pdfs"]["type"] == "function"
    assert schemas["search_pdfs"]["function"]["parameters"]["required"] == ["query"]
    assert schemas["read_pdf_page"]["function"]["parameters"]["required"] == ["doc_id", "page_number"]
    assert "sqlite" not in json.dumps(list(schemas.values())).lower()


def test_an_unknown_tool_and_bad_arguments_are_results_not_crashes(tmp_path) -> None:
    out, _key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)

    assert "error" in tools.call("delete_everything", {})
    assert "error" in tools.call("search_pdfs", {"query": "   "})
    assert "error" in tools.call("read_pdf_page", {"doc_id": "../etc", "page_number": 1})
    assert "error" in tools.call("read_pdf_page", {"doc_id": "0" * 16, "page_number": "many"})


def test_the_loop_round_trips_tool_results_and_cites_only_returned_evidence(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    model = FakeChatModel(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-1", "search_pdfs", {"query": "alpine", "top_k": 2})],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-2", "read_pdf_page", {"doc_id": key, "page_number": 1})],
            },
            {
                "role": "assistant",
                "content": (
                    f"The accepted page says the observation was alpine meteorology [{key} p.1]. "
                    f"It also does not support a claim about [{'f' * 16} p.7]."
                ),
            },
            {
                "role": "assistant",
                "content": f"The accepted page says the observation was alpine meteorology [{key} p.1].",
            },
        ]
    )
    events = []

    answer = run_agent("What does the accepted page say?", model, tools, on_event=events.append)

    assert answer.answer.startswith("The accepted page says")
    assert [citation.doc_id for citation in answer.citations] == [key]
    assert answer.citations[0].page_number == 1
    assert answer.citations[0].source_url == URL
    assert answer.citations[0].verified, "read_pdf_page returned this exact page"
    assert [outcome.name for outcome in answer.tool_calls] == ["search_pdfs", "read_pdf_page"]
    assert answer.tool_calls[0].result["hits"][0]["text"].startswith("alpine meteorology")
    assert answer.tool_calls[1].result["status"] == "accepted"
    assert answer.tool_calls[1].result["text"].startswith("alpine meteorology")
    assert not answer.budget_exhausted
    assert not answer.insufficient_evidence
    assert [event["tool"] for event in events] == ["search_pdfs", "read_pdf_page"]

    # The OpenAI round trip: the second request carries the tool result, and the first carries the schemas.
    second_turn = model.requests[1]["messages"]
    tool_messages = [message for message in second_turn if message["role"] == "tool"]
    assert tool_messages[0]["tool_call_id"] == "call-1"
    assert json.loads(tool_messages[0]["content"])["hits"][0]["doc_id"] == key
    assert model.requests[0]["tools"]
    assert model.requests[0]["messages"][0]["role"] == "system"


def test_citations_accept_a_quoted_doc_id_and_resolve_a_unique_page_only_tag(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    model = FakeChatModel(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-1", "search_pdfs", {"query": "alpine"})],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-2", "read_pdf_page", {"doc_id": key, "page_number": 1})],
            },
            {
                "role": "assistant",
                "content": (
                    f'Quoted evidence <Cite doc_id "{key}" p.1></Cite> and page '
                    "<doc_id> p.1</doc_id>."
                ),
            },
        ]
    )

    answer = run_agent("Cite it", model, tools)

    assert [citation.citation for citation in answer.citations] == [f"{key} p.1"]
    assert answer.citations[0].verified


def test_a_page_only_tag_only_resolves_to_a_page_that_was_read(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    unsupported = (
        f"The observation was alpine meteorology [{key} p.1]. "
        "A further claim rests on <doc_id> p.99</doc_id>."
    )
    model = FakeChatModel(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-1", "search_pdfs", {"query": "alpine"})],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-2", "read_pdf_page", {"doc_id": key, "page_number": 1})],
            },
            {"role": "assistant", "content": unsupported},
            {"role": "assistant", "content": unsupported},
        ]
    )

    answer = run_agent("Cite it", model, tools)

    assert [citation.citation for citation in answer.citations] == [f"{key} p.1"]
    assert "p.99" not in answer.answer
    assert not answer.insufficient_evidence


def test_the_tool_budget_bounds_the_loop_and_forces_a_final_answer(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    endless = {
        "role": "assistant",
        "content": None,
        "tool_calls": [_tool_call("call-1", "search_pdfs", {"query": "alpine"})],
    }
    model = FakeChatModel(
        [
            endless,
            {**endless, "tool_calls": [_tool_call("call-2", "read_pdf_page", {"doc_id": key, "page_number": 1})]},
            {"role": "assistant", "content": f"Best evidence is [{key} p.1]."},
        ]
    )

    answer = run_agent("Keep looking", model, tools, max_tool_calls=2)

    assert answer.budget_exhausted
    assert len(answer.tool_calls) == 2
    assert len(model.requests) == 3, "two tool turns and one forced answer"
    assert model.requests[-1]["tools"] == [], "the forced answer cannot ask for more tools"
    assert answer.answer == f"Best evidence is [{key} p.1]."
    assert [citation.citation for citation in answer.citations] == [f"{key} p.1"]
    assert answer.citations[0].verified, "the forced answer cites the page the second call read"
    assert MAX_TOOL_CALLS == 6


def test_the_default_budget_is_six_calls_per_question(tmp_path) -> None:
    out, _key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    model = FakeChatModel(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call(f"call-{index}", "search_pdfs", {"query": "alpine"}) for index in range(10)
                ],
            },
            {"role": "assistant", "content": "INSUFFICIENT EVIDENCE: no page answers this."},
        ]
    )

    answer = run_agent("Anything", model, tools)

    assert len(answer.tool_calls) == MAX_TOOL_CALLS
    assert answer.budget_exhausted


def test_an_unanswerable_question_reports_insufficient_evidence_without_citations(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    model = FakeChatModel(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-1", "search_pdfs", {"query": "quantum chromodynamics"})],
            },
            {
                "role": "assistant",
                "content": (
                    "INSUFFICIENT EVIDENCE: no accepted page mentions quantum chromodynamics. "
                    f"The nearest page only names alpine meteorology [{key} p.1]."
                ),
            },
        ]
    )

    answer = run_agent("What is the coupling constant?", model, tools)

    assert answer.insufficient_evidence
    assert answer.citations == ()
    assert f"{key} p.1" not in answer.answer, "an insufficient-evidence reply cites nothing"
    assert answer.tool_calls[0].result["total"] == 0


def test_an_empty_question_is_rejected_before_any_model_call() -> None:
    model = FakeChatModel([])

    with pytest.raises(ValueError, match="question"):
        run_agent(
            "   ",
            model,
            AcceptedEvidenceTools(RecordingIndex(), DiskPageReader(Path("/nowhere")), lambda: NO_DECLARED),
        )

    assert model.requests == []


def test_a_citation_for_a_page_no_read_returned_is_repaired_then_stripped(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    unsupported = (
        f"The accepted page says alpine meteorology [{key} p.1]. "
        f"An unread page supposedly adds a storm flight identifier [{key} p.2]."
    )
    model = FakeChatModel(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-1", "search_pdfs", {"query": "alpine"})],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-2", "read_pdf_page", {"doc_id": key, "page_number": 1})],
            },
            {"role": "assistant", "content": unsupported},
            {"role": "assistant", "content": unsupported},
        ]
    )

    answer = run_agent("What does the accepted page say?", model, tools)

    assert [citation.citation for citation in answer.citations] == [f"{key} p.1"]
    assert answer.citations[0].verified
    assert f"{key} p.2" not in answer.answer, "a page no tool returned cannot stay in the answer"
    assert "[]" not in answer.answer
    assert not answer.insufficient_evidence
    assert len(model.requests) == 4, "one repair turn, then the unanswered citation is stripped"
    assert model.requests[3]["messages"][-1]["role"] == "user"


def test_a_prose_only_page_reference_triggers_one_repair_turn_that_reads_the_page(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    model = FakeChatModel(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-1", "search_pdfs", {"query": "alpine"})],
            },
            {"role": "assistant", "content": "The observation is described on page 1."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-2", "read_pdf_page", {"doc_id": key, "page_number": 1})],
            },
            {
                "role": "assistant",
                "content": f"The observation was alpine meteorology [{key} p.1].",
            },
        ]
    )

    answer = run_agent("What does the accepted page say?", model, tools)

    assert [citation.citation for citation in answer.citations] == [f"{key} p.1"]
    assert answer.citations[0].verified
    assert not answer.insufficient_evidence
    assert [outcome.name for outcome in answer.tool_calls] == ["search_pdfs", "read_pdf_page"]
    assert len(model.requests) == 4, "two scripted answers, one repair turn and one final answer"
    repair = model.requests[2]["messages"][-1]
    assert repair["role"] == "user"
    assert "read_pdf_page" in repair["content"]


def test_a_supported_answer_drops_a_contradictory_insufficient_evidence_tail(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    model = FakeChatModel(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-1", "search_pdfs", {"query": "alpine"})],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-2", "read_pdf_page", {"doc_id": key, "page_number": 1})],
            },
            {
                "role": "assistant",
                "content": (
                    f"The observation was alpine meteorology [{key} p.1].\n\n"
                    "INSUFFICIENT EVIDENCE: the tools do not give a numeric temperature.\n"
                    "The search results do state the observation, so the answer above stands."
                ),
            },
        ]
    )

    answer = run_agent("What does the accepted page say?", model, tools)

    assert answer.answer.startswith("The observation was alpine meteorology")
    assert "INSUFFICIENT EVIDENCE" not in answer.answer
    assert not answer.insufficient_evidence
    assert [citation.citation for citation in answer.citations] == [f"{key} p.1"]


def test_an_answer_with_no_verified_citation_becomes_insufficient_evidence(tmp_path) -> None:
    out, key, _duplicate = _accepted_output(tmp_path)
    tools = _tools(out)
    model = FakeChatModel(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-1", "search_pdfs", {"query": "alpine"})],
            },
            {"role": "assistant", "content": "The observation is described on page 2."},
            {"role": "assistant", "content": f"The observation is described on page 2 [{key} p.2]."},
        ]
    )

    answer = run_agent("What does the accepted page say?", model, tools)

    assert answer.insufficient_evidence
    assert answer.citations == ()
    assert answer.answer.startswith("INSUFFICIENT EVIDENCE")
    assert f"{key} p.2" not in answer.answer
    assert len(model.requests) == 3, "one repair turn, then the contract refuses the answer"


def test_the_search_tool_refuses_stale_evidence_instead_of_returning_it(tmp_path) -> None:
    """The agent may not return stale hits either: an out-of-date index is an explicit unavailable."""
    out, _key, _duplicate = _accepted_output(tmp_path)
    declared = DeclaredChunkSet.of(
        chunk
        for page in (
            PageRecord(document_key(URL), URL, DIGEST, 1, "alpine meteorology", "pymupdf@1"),
        )
        for chunk in chunk_page_records((page,), language="eng")
    )
    reindex_search(SqliteFtsSearchIndex(out / "search.db"), declared)
    tools = AcceptedEvidenceTools(
        SqliteFtsSearchIndex(out / "search.db"), DiskPageReader(out), lambda: DeclaredChunkSet.of(())
    )

    result = tools.call("search_pdfs", {"query": "alpine"})

    assert result["available"] is False
    assert result["hits"] == [], "no stale chunk text reaches the model"
    assert "index-search" in result["reason"]
