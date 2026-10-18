"""The browser explorer: a read-only view over the declared outputs.

The fixture writes the same files a completed run declares -- summary, records, pages and
exclusions -- plus a corpus manifest that maps each digest to its retained PDF. Nothing here runs the
pipeline: these tests prove the view reads what the flow wrote, maps PDFs safely, rejects arbitrary
paths, and never writes.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from finepdfs.application.search import reindex_search
from finepdfs.domain.chunks import chunk_page_records
from finepdfs.domain.publishing import PageRecord
from finepdfs.infrastructure.corpus.index import document_key
from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path
from finepdfs.infrastructure.publishing.disk import PublishedCorpus
from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex
from finepdfs.interfaces import explorer as explorer_module
from finepdfs.interfaces.explorer import Explorer, ExplorerError, explorer_for

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
URL = "https://a.example/one.pdf"
EXCLUDED_URL = "https://a.example/duplicate.pdf"
SECOND_EXCLUDED_URL = "https://a.example/failed.pdf"
SUMMARY = {
    "schema_version": 1,
    # Deliberately not the file counts below, and deliberately no timing: the browser shows the
    # measured, reproducible summary the flow declared.
    "corpus": {"crawl_id": "CC-MAIN-2026-39", "domain": "a.example", "crawl_records": 19, "documents": 18},
    "routing": {"documents": 18, "native": 16, "ocr": 2},
    "extraction": {"requested": 18, "calls": 0, "reused": 18},
    "published": {"documents": 17, "page_records": 313},
    "exclusions": {
        "total": 3,
        "by_kind": {"dropped": 1, "exact-duplicate": 1, "failed": 1, "near-duplicate": 0},
    },
    "engine": {"name": "docling", "identity": "docling@cli:docling:v2.140.0"},
}


def _write_corpus(corpus: Path, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    (corpus / "pdfs").mkdir(parents=True, exist_ok=True)
    (corpus / relative_pdf_path(digest)).write_bytes(body)
    manifest = CorpusManifest(
        crawl_id="CC-MAIN-2026-39",
        domain="a.example",
        entries=[
            CorpusEntry(
                url=URL,
                digest=digest,
                file=relative_pdf_path(digest),
                bytes=len(body),
                crawl_id="CC-MAIN-2026-39",
                warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
                offset=1,
                length=len(body),
            )
        ],
    )
    manifest.save(corpus / "manifest.json")
    return digest


def _completed_output(tmp_path: Path) -> tuple[Path, Path, str]:
    body = (FIXTURES / "warc_payload.bin").read_bytes()
    out = tmp_path / "out"
    corpus = tmp_path / "corpus"
    digest = _write_corpus(corpus, body)
    key = document_key(URL)

    (out / "records").mkdir(parents=True)
    (out / "pages").mkdir()
    (out / "excluded").mkdir()
    (out / "run-summary.json").write_text(json.dumps(SUMMARY, indent=2) + "\n", encoding="utf-8")
    (out / "records" / f"{key}.json").write_text(
        json.dumps(
            {
                "url": URL,
                "digest": digest,
                "engine": "pymupdf@1.28.2",
                "routing_decision": "text-layer",
                "routing_reason": "the text layer is healthy: 1139 characters per page across 100% of pages",
                "language": "Latn",
                "page_count": 2,
                "accepted_pages": 2,
                "dropped_pages": 0,
                "character_count": 42,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "pages" / f"{key}.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "doc_id": key,
                    "source_url": URL,
                    "digest": digest,
                    "page_number": number,
                    "text": f"accepted text for page {number}",
                    "engine": "pymupdf@1.28.2",
                }
            )
            for number in (2, 1)
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "excluded" / f"{document_key(EXCLUDED_URL)}.json").write_text(
        json.dumps(
            {
                "url": EXCLUDED_URL,
                "key": document_key(EXCLUDED_URL),
                "kind": "exact-duplicate",
                "reason": f"identical bytes to {URL}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "excluded" / f"{document_key(SECOND_EXCLUDED_URL)}.json").write_text(
        json.dumps(
            {
                "url": SECOND_EXCLUDED_URL,
                "key": document_key(SECOND_EXCLUDED_URL),
                "kind": "failed",
                "reason": "these bytes are not a readable PDF: Failed to open stream",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return out, corpus, digest


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_the_explorer_returns_the_declared_summary_verbatim(tmp_path) -> None:
    out, corpus, _ = _completed_output(tmp_path)

    explorer = explorer_for(out, corpus)

    assert explorer.summary() == SUMMARY


def test_a_missing_or_invalid_summary_is_a_clean_error(tmp_path) -> None:
    with pytest.raises(ExplorerError, match="no completed output"):
        explorer_for(tmp_path / "missing", tmp_path / "corpus")

    out = tmp_path / "out"
    out.mkdir()
    (out / "run-summary.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ExplorerError, match="not valid JSON"):
        explorer_for(out, tmp_path / "corpus")


def test_a_broken_manifest_is_a_clean_error(tmp_path) -> None:
    out, corpus, _ = _completed_output(tmp_path)
    (corpus / "manifest.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ExplorerError, match="corpus manifest"):
        explorer_for(out, corpus)


def test_the_status_table_has_published_documents_and_exclusions(tmp_path) -> None:
    out, corpus, digest = _completed_output(tmp_path)

    documents = {document["key"]: document for document in explorer_for(out, corpus).documents()}

    published = documents[document_key(URL)]
    assert published["status"] == "published"
    assert published["url"] == URL
    assert published["language"] == "Latn"
    assert published["routing_decision"] == "text-layer"
    assert published["engine"] == "pymupdf@1.28.2"
    assert published["page_count"] == 2
    assert published["accepted_pages"] == 2
    assert published["pdf_url"] == f"/pdf/{digest}.pdf"
    assert "accepted text for page 2" in published["text"]

    duplicate = documents[document_key(EXCLUDED_URL)]
    assert duplicate["status"] == "exact-duplicate"
    assert duplicate["exclusion_reason"] == f"identical bytes to {URL}"
    assert duplicate["pdf_url"] is None

    failed = documents[document_key(SECOND_EXCLUDED_URL)]
    assert failed["status"] == "failed"
    assert "not a readable PDF" in failed["exclusion_reason"]


def test_a_document_detail_carries_each_accepted_page_and_its_original_page_link(tmp_path) -> None:
    out, corpus, digest = _completed_output(tmp_path)
    explorer = explorer_for(out, corpus)

    detail = explorer.document(document_key(URL))

    assert [page["page_number"] for page in detail["pages"]] == [1, 2]
    assert [page["text"] for page in detail["pages"]] == [
        "accepted text for page 1",
        "accepted text for page 2",
    ]
    assert detail["pages"][1]["pdf_url"] == f"/pdf/{digest}.pdf#page=2"
    assert detail["pdf_url"] == f"/pdf/{digest}.pdf"

    with pytest.raises(ExplorerError, match="no document"):
        explorer.document("0" * 16)


def test_an_exclusion_has_a_reason_but_no_pages(tmp_path) -> None:
    out, corpus, _ = _completed_output(tmp_path)

    detail = explorer_for(out, corpus).document(document_key(EXCLUDED_URL))

    assert detail["status"] == "exact-duplicate"
    assert detail["pages"] == []
    assert detail["routing_reason"] is None


def test_the_table_text_is_the_accepted_pages_in_original_page_order(tmp_path) -> None:
    """The export may be written in any order; one page parser puts both views in original page order.

    The status table and the document detail read the same declared page export. Two parsers let the
    table's text and the detail's pages disagree about which page is which, and only one of them keeps
    the contract: accepted Page Texts in original page order.
    """
    out, corpus, _digest = _completed_output(tmp_path)

    explorer = explorer_for(out, corpus)
    published = {row["key"]: row for row in explorer.documents()}[document_key(URL)]
    detail = explorer.document(document_key(URL))

    assert published["text"] == "\n".join(page["text"] for page in detail["pages"])
    assert detail["pages"][0]["page_number"] == 1


def test_a_document_detail_and_its_status_row_agree(tmp_path) -> None:
    """One row builder: the table's row and the detail are the same declared state, read once."""
    out, corpus, _digest = _completed_output(tmp_path)

    explorer = explorer_for(out, corpus)
    for row in explorer.documents():
        detail = explorer.document(row["key"])
        shared = {name: value for name, value in row.items() if name in detail}

        assert {name: detail[name] for name in shared} == shared


def test_the_pdf_is_served_only_when_the_manifest_names_its_digest(tmp_path) -> None:
    out, corpus, digest = _completed_output(tmp_path)
    explorer = explorer_for(out, corpus)

    assert explorer.pdf_path(digest) == corpus / relative_pdf_path(digest)
    assert explorer.pdf_path("f" * 64) is None
    assert explorer.pdf_path("../manifest.json") is None
    assert explorer.pdf_path("0" * 64) is None


def test_the_pdf_mapping_follows_a_completed_manifest_update(tmp_path) -> None:
    """A running explorer re-reads the manifest, so an add/remove does not leave stale PDF links."""
    out, corpus, digest = _completed_output(tmp_path)
    explorer = explorer_for(out, corpus)
    assert explorer.document(document_key(EXCLUDED_URL))["pdf_url"] is None

    manifest = CorpusManifest.load(corpus / "manifest.json")
    body = (FIXTURES / "warc_payload.bin").read_bytes()
    manifest.add(
        CorpusEntry(
            url=EXCLUDED_URL,
            digest=digest,
            file=relative_pdf_path(digest),
            bytes=len(body),
            crawl_id="CC-MAIN-2026-39",
            warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
            offset=2,
            length=len(body),
        )
    )
    manifest.save(corpus / "manifest.json")

    assert explorer.document(document_key(EXCLUDED_URL))["pdf_url"] == f"/pdf/{digest}.pdf"


def test_the_http_api_returns_summary_records_pages_and_exclusions(tmp_path) -> None:
    out, corpus, digest = _completed_output(tmp_path)
    key = document_key(URL)

    with _serving(explorer_for(out, corpus)) as base:
        assert _json(base + "/api/summary") == SUMMARY
        records = _json(base + "/api/records")
        assert records["records"][0]["url"] == URL
        assert records["records"][0]["key"] == key
        exclusions = _json(base + "/api/exclusions")
        assert {entry["kind"] for entry in exclusions["exclusions"]} == {"exact-duplicate", "failed"}
        pages = _json(base + f"/api/pages/{key}")
        assert [page["page_number"] for page in pages["pages"]] == [1, 2]
        assert pages["pages"][1]["pdf_url"] == f"/pdf/{digest}.pdf#page=2"
        detail = _json(base + f"/api/documents/{key}")
        assert detail["url"] == URL
        assert len(detail["pages"]) == 2

        pdf = _get(base + f"/pdf/{digest}.pdf")
        assert pdf.status == 200
        assert pdf.headers["Content-Type"] == "application/pdf"
        assert pdf.read() == (FIXTURES / "warc_payload.bin").read_bytes()


def test_unknown_files_and_path_traversal_are_rejected(tmp_path) -> None:
    out, corpus, _ = _completed_output(tmp_path)

    with _serving(explorer_for(out, corpus)) as base:
        for path in (
            "/../../etc/passwd",
            "/api/pages/not-a-key",
            "/api/pages/../records/x.json",
            "/pdf/..%2F..%2Fetc%2Fpasswd.pdf",
            "/pdf/manifest.json",
            "/api/unknown",
        ):
            with pytest.raises(urllib.error.HTTPError) as caught:
                _get(base + path)
            assert caught.value.code == 404
            assert "error" in json.loads(caught.value.read())


def test_serving_the_explorer_writes_nothing(tmp_path) -> None:
    out, corpus, digest = _completed_output(tmp_path)
    before = {root: _snapshot(root) for root in (out, corpus)}

    with _serving(explorer_for(out, corpus)) as base:
        _get(base + "/")
        _json(base + "/api/documents")
        _json(base + "/api/summary")
        _get(base + f"/pdf/{digest}.pdf")

    after = {root: _snapshot(root) for root in (out, corpus)}
    assert after == before


def test_the_page_asset_is_its_own_module(tmp_path) -> None:
    """The HTML/CSS/JS asset sits beside the explorer, not inside the export-reading code it is served by."""
    from finepdfs.interfaces import explorer_page

    out, corpus, _digest = _completed_output(tmp_path)

    assert explorer_page.EXPLORER_HTML is explorer_module.EXPLORER_HTML
    with _serving(explorer_for(out, corpus)) as base:
        assert _get(base + "/").read().decode("utf-8") == explorer_page.EXPLORER_HTML


def test_the_html_carries_the_filter_and_detail_contract(tmp_path) -> None:
    out, corpus, _digest = _completed_output(tmp_path)

    with _serving(explorer_for(out, corpus)) as base:
        page = _get(base + "/")
        html = page.read().decode("utf-8")
        assert page.status == 200
        assert page.headers["Content-Type"].startswith("text/html")
        for identifier in (
            'id="filter-url"',
            'id="filter-language"',
            'id="filter-routing"',
            'id="filter-kind"',
            'id="status-table"',
            'id="document-detail"',
        ):
            assert identifier in html
        assert "read-only" in html
        assert "/api/documents" in html
        assert "finished_at" in html
        assert "not recorded" in html
        assert "#page=" in html
        assert "Engine Identity" in html
        assert "Accepted pages" in html


def _js_function(html: str, start: str, end: str) -> str:
    """The served asset's text from one function marker to the next, for a contract assertion."""
    return html.split(start, 1)[1].split(end, 1)[0]


def test_the_page_asset_renders_marks_and_rechecks_search_on_every_reload(tmp_path) -> None:
    """A highlighted snippet must become a <mark>, and a reload must re-ask the active search.

    The API already answers a stale index with ``available: false``, but the page has to ask again:
    ``loadAll`` used to reload the table and detail while leaving the previous hits labelled
    "accepted, current chunks". There is no browser framework in the offline suite, so this pins
    the asset's two contracts; the runbook's real-browser check is the behavioural evidence.
    """
    out, corpus, _digest = _completed_output(tmp_path)

    with _serving(explorer_for(out, corpus)) as base:
        html = _get(base + "/").read().decode("utf-8")

    split = _js_function(html, "function snippetFragment(", "function searchHit(")
    assert 'split(new RegExp("(<mark>|</mark>)"))' in split, (
        "split() must capture the delimiters, or the loop never sees them and every part renders plain"
    )
    assert 'textNode(highlighted ? "mark" : "span", part)' in split

    load_all = _js_function(html, "async function loadAll(", "function setError(")
    assert "await reloadSearch()" in load_all, (
        "a reload must not leave the old hits showing as accepted, current chunks"
    )

    search = _js_function(html, "async function searchEvidence(", "function currentSearchParams(")
    assert 'state.searchParams = "";' in search and 'byId("search-results").replaceChildren()' in search, (
        "an empty submit must drop the earlier hits instead of keeping them labelled current"
    )


def test_absent_run_timing_is_reported_as_not_recorded(tmp_path) -> None:
    """Timing is observed beside the outputs, never declared: its absence is a state, not a zero."""
    out, corpus, _ = _completed_output(tmp_path)

    with _serving(explorer_for(out, corpus)) as base:
        observed = _json(base + "/api/observed-run")

        assert observed["recorded"] is False
        assert observed["elapsed_seconds"] is None
        assert "not recorded" in observed["note"]


def test_a_recorded_observation_carries_elapsed_labelled_observed(tmp_path) -> None:
    out, corpus, _ = _completed_output(tmp_path)
    (out / "last-run.json").write_text(
        json.dumps(
            {
                "observed": True,
                "note": "observed wall-clock timing of one run; it is not declared target state and is not reproducible",
                "started_at": "2026-10-18T02:07:42Z",
                "finished_at": "2026-10-18T02:07:51Z",
                "elapsed_seconds": 8.311,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with _serving(explorer_for(out, corpus)) as base:
        observed = _json(base + "/api/observed-run")
        summary = _json(base + "/api/summary")

    assert observed["recorded"] is True
    assert observed["elapsed_seconds"] == 8.311
    assert observed["finished_at"] == "2026-10-18T02:07:51Z"
    assert "not declared target state" in observed["note"]
    assert summary == SUMMARY, "the observation never leaks into the declared summary"


def test_a_zero_byte_index_is_unavailable_and_stays_zero_bytes(tmp_path) -> None:
    """Serving the explorer must not create, rebuild or migrate the index it reads."""
    out, corpus, _digest = _completed_output(tmp_path)
    index_path = out / "search.db"
    index_path.write_bytes(b"")

    with _serving(explorer_for(out, corpus)) as base:
        payload = _json(base + "/api/search?query=alpine")

    assert payload["available"] is False
    assert payload["hits"] == []
    assert "index-search" in payload["reason"]
    assert index_path.read_bytes() == b"", "a read-only reader leaves a 0-byte index alone"


def test_a_stale_index_is_unavailable_until_it_is_rebuilt(tmp_path) -> None:
    out, corpus, _digest = _completed_output(tmp_path)
    key = document_key(URL)
    index_path = _write_search_index(out, key, _digest)
    assert _json_path(index_path, out)["available"] is True

    # The flow declares a replacement: the chunk file on disk now differs from what was indexed.
    (out / "chunks" / f"{key}.jsonl").unlink()

    with _serving(explorer_for(out, corpus)) as base:
        payload = _json(base + "/api/search?query=alpine")

    assert payload["available"] is False
    assert payload["hits"] == [], "removed evidence is never served from a stale index"
    assert "stale" in payload["reason"]
    assert "index-search" in payload["reason"]


def _json_path(index_path: Path, out: Path) -> dict:
    """Read the index through the same contract the browser does, without a server."""
    from finepdfs.application.search import SearchAcceptedEvidence
    from finepdfs.infrastructure.publishing.disk import PublishedCorpus
    from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex

    declared = PublishedCorpus(out)
    return SearchAcceptedEvidence(
        SqliteFtsSearchIndex(index_path), declared.read_declared_chunks
    ).search("alpine").as_json()


def test_search_returns_the_shared_contract_from_the_index(tmp_path) -> None:
    out, corpus, digest = _completed_output(tmp_path)
    key = document_key(URL)
    index_path = _write_search_index(out, key, digest)
    assert index_path.is_file()

    with _serving(explorer_for(out, corpus)) as base:
        payload = _json(base + "/api/search?query=alpine&top_k=5")
        assert payload["available"] is True
        assert payload["query"] == "alpine"
        assert payload["total"] == 1
        hit = payload["hits"][0]
        assert hit["doc_id"] == key
        assert hit["page_number"] == 1
        assert hit["source_url"] == URL
        assert hit["digest"] == digest
        assert hit["chunk_id"]
        assert hit["text"] == "alpine meteorology observation on an accepted page"
        assert "<mark>" in hit["snippet"]
        assert hit["score"] > 0

        assert _json(base + "/api/search?query=alpine&language=eng")["total"] == 1
        assert _json(base + "/api/search?query=alpine&language=spa")["total"] == 0
        assert _json(base + f"/api/search?query=alpine&doc_id={key}")["total"] == 1
        assert _json(base + "/api/search?query=alpine&doc_id=" + "0" * 16)["total"] == 0
        assert _json(base + "/api/search?query=alpine&engine=pymupdf%401.28.2")["total"] == 1
        assert _json(base + "/api/search?query=alpine&engine=other")["total"] == 0


def test_search_without_an_index_is_a_clear_empty_state(tmp_path) -> None:
    out, corpus, _digest = _completed_output(tmp_path)
    index_path = out / "search.db"

    with _serving(explorer_for(out, corpus)) as base:
        payload = _json(base + "/api/search?query=alpine")

        assert payload["available"] is False
        assert payload["total"] == 0
        assert payload["hits"] == []
        assert "index-search" in payload["reason"]
        assert not index_path.exists(), "a read-only view must not build the index it reads"


def test_search_rejects_an_empty_query_and_a_bad_top_k(tmp_path) -> None:
    out, corpus, digest = _completed_output(tmp_path)
    _write_search_index(out, document_key(URL), digest)

    with _serving(explorer_for(out, corpus)) as base:
        for path in ("/api/search", "/api/search?query=%20%20", "/api/search?query=alpine&top_k=0",
                     "/api/search?query=alpine&top_k=not-a-number"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                _get(base + path)
            assert caught.value.code == 400
            assert "error" in json.loads(caught.value.read())


def test_searching_the_explorer_writes_nothing(tmp_path) -> None:
    out, corpus, digest = _completed_output(tmp_path)
    _write_search_index(out, document_key(URL), digest)
    before = {root: _snapshot(root) for root in (out, corpus)}

    with _serving(explorer_for(out, corpus)) as base:
        _json(base + "/api/search?query=alpine")

    assert {root: _snapshot(root) for root in (out, corpus)} == before


def _write_search_index(out: Path, key: str, digest: str) -> Path:
    pages = (
        PageRecord(
            doc_id=key,
            source_url=URL,
            digest=digest,
            page_number=1,
            text="alpine meteorology observation on an accepted page",
            engine="pymupdf@1.28.2",
        ),
        PageRecord(
            doc_id=key,
            source_url=URL,
            digest=digest,
            page_number=2,
            text="a storm flight identifier on the second accepted page",
            engine="pymupdf@1.28.2",
        ),
    )
    chunks = tuple(
        chunk for page in pages for chunk in chunk_page_records((page,), language="eng")
    )
    # The flow's declared chunk export is the index's only input, so the fixture declares it too:
    # an index built from anything else is stale by definition.
    declared = out / "chunks"
    declared.mkdir(exist_ok=True)
    (declared / f"{key}.jsonl").write_text(
        PublishedCorpus(out).chunks_payload(chunks), encoding="utf-8"
    )
    index_path = out / "search.db"
    reindex_search(SqliteFtsSearchIndex(index_path), PublishedCorpus(out).read_declared_chunks())
    return index_path


def test_the_html_carries_the_search_contract(tmp_path) -> None:
    out, corpus, _digest = _completed_output(tmp_path)

    with _serving(explorer_for(out, corpus)) as base:
        html = _get(base + "/").read().decode("utf-8")

    for identifier in (
        'id="search-form"',
        'id="search-query"',
        'id="search-language"',
        'id="search-results"',
        'id="search-state"',
    ):
        assert identifier in html
    assert "/api/search" in html
    assert "Accepted evidence search" in html


def test_the_cli_explore_command_takes_the_output_and_port() -> None:
    from finepdfs.interfaces.cli import build_parser

    args = build_parser().parse_args(
        ["explore", "--out", "o", "--corpus", "c", "--port", "8123"]
    )

    assert args.command == "explore"
    assert args.out == Path("o")
    assert args.corpus == Path("c")
    assert args.port == 8123
    assert args.host == "127.0.0.1"
    assert args.index is None


def test_the_cli_explore_command_takes_an_index_path() -> None:
    from finepdfs.interfaces.cli import build_parser

    args = build_parser().parse_args(
        ["explore", "--out", "o", "--corpus", "c", "--index", "o/search.db"]
    )

    assert args.index == Path("o/search.db")


def test_the_cli_refuses_an_absent_output_without_starting_a_server(tmp_path, capsys, monkeypatch) -> None:
    from finepdfs.interfaces import cli

    def must_not_serve(*args, **kwargs):
        raise AssertionError("the explorer must not start without completed outputs")

    monkeypatch.setattr(explorer_module, "serve_explorer", must_not_serve)
    args = cli.build_parser().parse_args(
        ["explore", "--out", str(tmp_path / "missing"), "--corpus", str(tmp_path / "corpus")]
    )

    assert cli._explore(args) == 2
    assert "no completed output" in capsys.readouterr().err


def test_a_range_request_returns_only_the_requested_bytes(tmp_path) -> None:
    out, corpus, digest = _completed_output(tmp_path)
    body = (FIXTURES / "warc_payload.bin").read_bytes()

    with _serving(explorer_for(out, corpus)) as base:
        request = urllib.request.Request(base + f"/pdf/{digest}.pdf", headers={"Range": "bytes=0-3"})
        response = urllib.request.urlopen(request)

        assert response.status == 206
        assert response.headers["Content-Range"] == f"bytes 0-3/{len(body)}"
        assert response.read() == body[:4]


@contextmanager
def _serving(explorer: Explorer):
    server = explorer_module.make_server(explorer, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _get(url: str):
    return urllib.request.urlopen(url)


def _json(url: str) -> dict:
    with urllib.request.urlopen(url) as response:
        assert response.headers["Content-Type"].startswith("application/json")
        return json.loads(response.read())


def test_a_stale_page_export_is_not_shown_as_accepted_text(tmp_path) -> None:
    """The view parses pages through the same reader read_pdf_page uses, not a weaker parser.

    A replacement leaves the page export naming an older digest until the run rewrites it; showing that
    text would let the browser serve exactly what read_pdf_page rejects as stale.
    """
    from finepdfs.application.page_reads import read_pdf_page
    from finepdfs.infrastructure.publishing.disk import DiskPageReader

    out, corpus, _digest = _completed_output(tmp_path)
    key = document_key(URL)
    path = out / "pages" / f"{key}.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row["digest"] = "f" * 64
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    explorer = explorer_for(out, corpus)
    detail = explorer.document(key)

    assert detail["pages"] == [], "the browser never shows text read_pdf_page rejects as stale"
    table = {row["key"]: row for row in explorer.documents()}
    assert table[key]["text"] == "", "the status table's text is the same accepted pages"
    answer = read_pdf_page(DiskPageReader(out), key, 1)
    assert answer.status == "unsupported"
    assert "stale" in answer.reason
