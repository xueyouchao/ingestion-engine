"""The command line surface: every command parses, and flags mean what they say."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finepdfs.interfaces.cli import build_parser


@pytest.mark.parametrize(
    "argv, expected_command",
    [
        (["fetch-corpus", "--domain", "noaa.gov"], "fetch-corpus"),
        (["restore-corpus", "--manifest", "pinned.json"], "restore-corpus"),
        (["inspect", "--corpus", "c"], "inspect"),
        (["dry-run", "--corpus", "c", "--engine", "mineru"], "dry-run"),
        (["run", "--corpus", "c", "--out", "o", "--engine", "paddleocr-vl"], "run"),
        (["index-search", "--out", "o", "--index", "i.db"], "index-search"),
        (["search", "--index", "i.db", "alpine"], "search"),
    ],
)
def test_every_command_keeps_its_own_name(argv, expected_command) -> None:
    args = build_parser().parse_args(argv)

    assert args.command == expected_command


def test_the_similarity_threshold_reaches_the_runtime(monkeypatch) -> None:
    from finepdfs.interfaces import cli

    args = build_parser().parse_args(["run", "--similarity-threshold", "0.55"])
    monkeypatch.setattr(cli, "runtime_from_environment", lambda: "a runtime")

    cli._runtime_from_args(args)

    assert args.similarity_threshold == 0.55
    import os

    assert os.environ["FINEPDFS_SIMILARITY_THRESHOLD"] == "0.55"


def test_engine_flags_do_not_shadow_the_command_name() -> None:
    args = build_parser().parse_args(["run", "--engine", "mineru", "--engine-command", "mineru", "--dpi", "150"])

    assert args.command == "run"
    assert args.engine == "mineru"
    assert args.engine_command == "mineru"
    assert args.dpi == 150


def test_an_unknown_engine_is_rejected() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--engine", "magic-ocr"])


def test_fetch_corpus_needs_a_domain() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch-corpus"])


def test_restore_corpus_needs_a_manifest() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["restore-corpus"])


def test_a_flag_that_was_not_given_leaves_the_environment_alone(monkeypatch) -> None:
    """The documented env-only setup must survive a run that passes no flags at all."""
    import os

    from finepdfs.interfaces import cli

    monkeypatch.setenv("FINEPDFS_OUT", "/data/out")
    monkeypatch.setenv("FINEPDFS_ENGINE", "paddleocr-vl")
    monkeypatch.setenv("FINEPDFS_SIMILARITY_THRESHOLD", "0.5")
    monkeypatch.setattr(cli, "runtime_from_environment", lambda: "a runtime")

    cli._runtime_from_args(build_parser().parse_args(["run"]))

    assert os.environ["FINEPDFS_OUT"] == "/data/out"
    assert os.environ["FINEPDFS_ENGINE"] == "paddleocr-vl"
    assert os.environ["FINEPDFS_SIMILARITY_THRESHOLD"] == "0.5"


def test_a_run_over_an_empty_corpus_refuses_to_delete_every_output(tmp_path, capsys) -> None:
    """An empty corpus is an empty target state, and target state deletes what it no longer declares."""
    from finepdfs.interfaces import cli

    args = build_parser().parse_args(
        ["run", "--corpus", str(tmp_path / "nothing"), "--out", str(tmp_path / "out")]
    )

    assert cli._run(args) == 2

    assert "refusing" in capsys.readouterr().err


def test_the_empty_corpus_case_can_be_meant_on_purpose() -> None:
    args = build_parser().parse_args(["run", "--allow-empty"])

    assert args.allow_empty is True


def test_out_comes_from_the_runtime_so_the_environment_can_set_it() -> None:
    args = build_parser().parse_args(["run", "--out", "somewhere"])

    assert args.out.name == "somewhere"
    assert not hasattr(build_parser().parse_args(["run"]), "out"), "an untyped flag must not appear at all"


def test_an_engine_command_failure_reaches_the_traceback(monkeypatch) -> None:
    """Only a run that cannot start is exit 2; an engine that fails mid-run keeps its traceback."""
    from finepdfs.infrastructure.engines.subprocess_support import CommandFailed
    from finepdfs.interfaces import cli

    def failing_engine(args):
        raise CommandFailed("the engine command failed")

    monkeypatch.setattr(cli, "_dispatch", failing_engine)

    with pytest.raises(CommandFailed):
        cli.main(["inspect"])


def test_a_run_prints_the_measured_summary_it_declared(monkeypatch, tmp_path, capsys) -> None:
    """The summary is a declared output; the terminal print is a view of it, not a second writer."""
    import hashlib

    from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path
    from finepdfs.interfaces import cli

    body = (Path(__file__).resolve().parent.parent / "fixtures" / "warc_payload.bin").read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    corpus_dir = tmp_path / "corpus"
    (corpus_dir / "pdfs").mkdir(parents=True)
    (corpus_dir / relative_pdf_path(digest)).write_bytes(body)
    CorpusManifest(
        crawl_id="CC-MAIN-2026-39",
        domain="a.example",
        entries=[
            CorpusEntry(
                url="https://a.example/one.pdf",
                digest=digest,
                file=relative_pdf_path(digest),
                bytes=len(body),
                crawl_id="CC-MAIN-2026-39",
                warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
                offset=1,
                length=len(body),
            )
        ],
    ).save(corpus_dir / "manifest.json")
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / "runs.db"))

    args = cli.build_parser().parse_args(
        ["run", "--corpus", str(corpus_dir), "--out", str(tmp_path / "out"), "--engine", "pymupdf"]
    )
    assert cli._run(args) == 0

    printed = capsys.readouterr().out
    assert "run summary:" in printed
    assert "extraction calls=1 reused=0" in printed
    assert "published=1 documents" in printed
    assert (tmp_path / "out" / "run-summary.json").exists()


def test_the_run_records_observed_timing_beside_the_declared_outputs(monkeypatch, tmp_path, capsys) -> None:
    """The flow declares reproducible counters; the command observes wall time in a file nobody declares."""
    import hashlib

    from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path
    from finepdfs.interfaces import cli

    body = (Path(__file__).resolve().parent.parent / "fixtures" / "warc_payload.bin").read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    corpus_dir = tmp_path / "corpus"
    (corpus_dir / "pdfs").mkdir(parents=True)
    (corpus_dir / relative_pdf_path(digest)).write_bytes(body)
    CorpusManifest(
        crawl_id="CC-MAIN-2026-39",
        domain="a.example",
        entries=[
            CorpusEntry(
                url="https://a.example/one.pdf",
                digest=digest,
                file=relative_pdf_path(digest),
                bytes=len(body),
                crawl_id="CC-MAIN-2026-39",
                warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
                offset=1,
                length=len(body),
            )
        ],
    ).save(corpus_dir / "manifest.json")
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / "runs.db"))
    out_dir = tmp_path / "out"
    argv = ["run", "--corpus", str(corpus_dir), "--out", str(out_dir), "--engine", "pymupdf"]

    # Three runs: the cold one is a different run, the next two are identical by construction.
    assert cli.main(argv) == 0
    assert cli.main(argv) == 0
    declared = (out_dir / "run-summary.json").read_bytes()
    assert cli.main(argv) == 0

    assert (out_dir / "run-summary.json").read_bytes() == declared, (
        "a rerun over the same state rewrites the same declared bytes"
    )
    observed = json.loads((out_dir / "last-run.json").read_text())
    assert observed["observed"] is True
    assert "not declared target state" in observed["note"]
    assert observed["elapsed_seconds"] >= 0
    assert observed["finished_at"].endswith("Z")
    assert "(observed)" in capsys.readouterr().out


def test_the_search_flags_reach_the_commands() -> None:
    index = build_parser().parse_args(
        ["search", "--index", "i.db", "--top-k", "3", "--language", "eng", "--engine", "docling@cli",
         "--doc-id", "abc", "alpine", "meteorology"]
    )
    search = build_parser().parse_args(["index-search", "--out", "o", "--index", "i.db"])

    assert index.query == ["alpine", "meteorology"]
    assert (index.index.name, index.top_k, index.language, index.engine, index.doc_id) == (
        "i.db",
        3,
        "eng",
        "docling@cli",
        "abc",
    )
    assert (search.out.name, search.index.name) == ("o", "i.db")


def _write_declared_chunks(out_dir: Path) -> dict:
    """A tiny declared output: the same chunk file the flow declares, written by the same code."""
    from finepdfs.domain.chunks import chunk_page_records
    from finepdfs.domain.publishing import PageRecord
    from finepdfs.infrastructure.corpus.index import document_key
    from finepdfs.infrastructure.publishing.disk import PublishedCorpus

    corpus = PublishedCorpus(out_dir)
    records = []
    for number, text in (
        (4, "Harbour dredging continues across the tidal range."),
        (9, "Alpine meteorology over the northern range."),
    ):
        url = f"https://example.org/page-{number}.pdf"
        record = PageRecord(
            doc_id=document_key(url),
            source_url=url,
            digest=str(number) * 64,
            page_number=number,
            text=text,
            engine="pymupdf@native",
        )
        records.append(record)
        corpus.record_path(record.doc_id).parent.mkdir(parents=True, exist_ok=True)
        corpus.record_path(record.doc_id).write_text(
            json.dumps(
                {
                    "url": record.source_url,
                    "digest": record.digest,
                    "engine": record.engine,
                    "routing_decision": "text-layer",
                    "routing_reason": "healthy text layer",
                    "language": "eng",
                    "page_count": 1,
                    "accepted_pages": 1,
                    "dropped_pages": 0,
                    "character_count": len(record.text),
                }
            ),
            encoding="utf-8",
        )
        corpus.pages_path(record.doc_id).parent.mkdir(parents=True, exist_ok=True)
        corpus.pages_path(record.doc_id).write_text(corpus.pages_payload((record,)), encoding="utf-8")
        path = corpus.chunks_path(record.doc_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(corpus.chunks_payload(chunk_page_records((record,), language="eng")))
    return {"chunks": 2, "documents": 2}


def test_the_fallback_search_is_usable_from_the_command_line(tmp_path, capsys) -> None:
    """The local backend ships with two commands: rebuild the index, then read the same contract."""
    from finepdfs.interfaces import cli

    out_dir = tmp_path / "out"
    _write_declared_chunks(out_dir)
    index = tmp_path / "search.db"

    assert cli.main(["index-search", "--out", str(out_dir), "--index", str(index)]) == 0
    printed = capsys.readouterr().out
    assert "2 chunks from 2 documents" in printed

    assert cli.main(["search", "--out", str(out_dir), "--index", str(index), "harbour"]) == 0
    printed = capsys.readouterr().out
    assert "p.4" in printed
    assert "https://example.org/page-4.pdf" in printed
    assert "<mark>Harbour</mark>" in printed

    assert cli.main(["search", "--out", str(out_dir), "--index", str(index), "--json", "harbour"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    hit = payload["hits"][0]
    assert set(hit) >= {"doc_id", "chunk_id", "source_url", "digest", "page_number", "text", "snippet", "score"}
    assert hit["page_number"] == 4

    assert cli.main(["search", "--out", str(out_dir), "--index", str(index), "--language", "fra", "harbour"]) == 0
    assert "no matches" in capsys.readouterr().out


def test_a_stale_index_is_refused_by_the_search_command(tmp_path, capsys) -> None:
    """A stale index is an operator state, not a hit list: no stale text reaches the terminal."""
    from finepdfs.interfaces import cli

    out_dir = tmp_path / "out"
    _write_declared_chunks(out_dir)
    index = tmp_path / "search.db"
    assert cli.main(["index-search", "--out", str(out_dir), "--index", str(index)]) == 0
    capsys.readouterr()

    for path in (out_dir / "chunks").glob("*.jsonl"):
        path.unlink()

    assert cli.main(["search", "--out", str(out_dir), "--index", str(index), "harbour"]) == 2
    captured = capsys.readouterr()
    assert "index-search" in captured.err
    assert "Harbour" not in captured.out, "no stale hit text is printed"


def test_the_search_index_path_comes_from_one_resolver_that_honours_the_environment(monkeypatch) -> None:
    from finepdfs.interfaces.search_paths import resolve_out_dir, resolve_search_index

    monkeypatch.setenv("FINEPDFS_SEARCH_INDEX", "/data/configured.db")
    monkeypatch.setenv("FINEPDFS_OUT", "/data/out")
    assert resolve_search_index() == Path("/data/configured.db")
    assert resolve_search_index(out=Path("o")) == Path("/data/configured.db"), (
        "the documented variable wins over the output directory's default name"
    )
    assert resolve_search_index(out=Path("o"), index=Path("flag.db")) == Path("flag.db"), (
        "an explicit flag wins over the environment"
    )
    assert resolve_out_dir() == Path("/data/out"), "the output directory honours its documented variable"
    assert resolve_out_dir(Path("o")) == Path("o"), "an explicit --out wins over the variable"

    monkeypatch.delenv("FINEPDFS_SEARCH_INDEX")
    monkeypatch.delenv("FINEPDFS_OUT")
    assert resolve_search_index(out=Path("o")) == Path("o/search.db")
    assert resolve_out_dir() == Path("out")


def _completed_output(out_dir: Path) -> None:
    """A declared output the explorer accepts: chunks are not needed, a summary and a corpus are."""
    _write_declared_chunks(out_dir)
    (out_dir / "run-summary.json").write_text("{}\n", encoding="utf-8")


def test_explore_takes_its_output_directory_and_index_from_the_environment(
    tmp_path, monkeypatch, capsys
) -> None:
    """explore's --out used to default to out/, so FINEPDFS_OUT was ignored; one resolver fixes it."""
    from finepdfs.interfaces import cli
    from finepdfs.interfaces import explorer as explorer_module

    out_dir = tmp_path / "published"
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    _completed_output(out_dir)
    monkeypatch.setenv("FINEPDFS_OUT", str(out_dir))
    served = []
    monkeypatch.setattr(explorer_module, "serve_explorer", lambda explorer, **kwargs: served.append(explorer))

    assert cli.main(["explore", "--corpus", str(corpus_dir)]) == 0

    assert served, "explore served a completed output"
    assert served[0].out_dir == out_dir, "FINEPDFS_OUT names the directory the explorer serves"
    assert served[0].index_path == out_dir / "search.db", "the index follows the same resolved output"
    assert "no search index" in capsys.readouterr().out


def test_explore_flags_win_over_the_environment(tmp_path, monkeypatch) -> None:
    from finepdfs.interfaces import cli
    from finepdfs.interfaces import explorer as explorer_module

    configured = tmp_path / "configured"
    flagged = tmp_path / "flagged"
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    _completed_output(configured)
    _completed_output(flagged)
    monkeypatch.setenv("FINEPDFS_OUT", str(configured))
    monkeypatch.setenv("FINEPDFS_SEARCH_INDEX", str(tmp_path / "configured.db"))
    served = []
    monkeypatch.setattr(explorer_module, "serve_explorer", lambda explorer, **kwargs: served.append(explorer))

    assert cli.main(
        [
            "explore",
            "--corpus", str(corpus_dir),
            "--out", str(flagged),
            "--index", str(flagged / "flag.db"),
        ]
    ) == 0

    assert served[0].out_dir == flagged
    assert served[0].index_path == flagged / "flag.db"


def test_ask_honours_the_documented_search_index_variable(tmp_path, capsys, monkeypatch) -> None:
    """ask used to ignore FINEPDFS_SEARCH_INDEX; it now resolves the path the same way search does."""
    from finepdfs.infrastructure.corpus.index import document_key
    from finepdfs.interfaces import cli

    out_dir = tmp_path / "out"
    _write_declared_chunks(out_dir)
    configured = tmp_path / "configured.db"
    assert cli.main(["index-search", "--out", str(out_dir), "--index", str(configured)]) == 0
    capsys.readouterr()
    monkeypatch.setenv("FINEPDFS_SEARCH_INDEX", str(configured))
    monkeypatch.setattr(
        cli, "ChatCompletionsClient", lambda *args, **kwargs: _ask_model(document_key("https://example.org/page-9.pdf"))
    )
    args = build_parser().parse_args(
        [
            "ask", "--question", "q", "--base-url", "http://[::1]:11434/v1", "--model", "m",
            "--out", str(out_dir),
        ]
    )

    assert cli._ask(args) == 0
    assert "no search index" not in capsys.readouterr().err


def test_ask_takes_its_output_directory_and_index_from_the_environment(
    tmp_path, capsys, monkeypatch
) -> None:
    """ask used to resolve --out to a literal out/, so FINEPDFS_OUT never named its pages or index."""
    from finepdfs.infrastructure.corpus.index import document_key
    from finepdfs.interfaces import cli

    out_dir = tmp_path / "published"
    _write_declared_chunks(out_dir)
    assert cli.main(["index-search", "--out", str(out_dir)]) == 0
    capsys.readouterr()
    monkeypatch.setenv("FINEPDFS_OUT", str(out_dir))
    monkeypatch.delenv("FINEPDFS_SEARCH_INDEX", raising=False)
    doc_id = document_key("https://example.org/page-9.pdf")
    monkeypatch.setattr(cli, "ChatCompletionsClient", lambda *args, **kwargs: _ask_model(doc_id))
    args = build_parser().parse_args(
        [
            "ask", "--question", "q", "--base-url", "http://[::1]:11434/v1", "--model", "m",
        ]
    )

    assert cli._ask(args) == 0

    printed = capsys.readouterr().out
    assert "no search index" not in printed
    assert f"- {doc_id} p.9" in printed, "the page read came from the same resolved output directory"


def test_ask_flags_win_over_the_environment(tmp_path, capsys, monkeypatch) -> None:
    from finepdfs.infrastructure.corpus.index import document_key
    from finepdfs.interfaces import cli

    configured = tmp_path / "configured"
    flagged = tmp_path / "flagged"
    _write_declared_chunks(configured)
    _write_declared_chunks(flagged)
    assert cli.main(["index-search", "--out", str(flagged), "--index", str(tmp_path / "flag.db")]) == 0
    capsys.readouterr()
    monkeypatch.setenv("FINEPDFS_OUT", str(configured))
    monkeypatch.setenv("FINEPDFS_SEARCH_INDEX", str(tmp_path / "configured.db"))
    doc_id = document_key("https://example.org/page-9.pdf")
    monkeypatch.setattr(cli, "ChatCompletionsClient", lambda *args, **kwargs: _ask_model(doc_id))
    args = build_parser().parse_args(
        [
            "ask", "--question", "q", "--base-url", "http://[::1]:11434/v1", "--model", "m",
            "--out", str(flagged), "--index", str(tmp_path / "flag.db"),
        ]
    )

    assert cli._ask(args) == 0
    assert f"- {doc_id} p.9" in capsys.readouterr().out


def test_indexing_a_directory_that_holds_no_run_is_an_operator_error(tmp_path, capsys) -> None:
    from finepdfs.interfaces import cli

    assert cli.main(["index-search", "--out", str(tmp_path / "nothing"), "--index", str(tmp_path / "i.db")]) == 2
    assert "no published output" in capsys.readouterr().err


class _FakeChatModel:
    """A scripted OpenAI-compatible model; each request is kept so the loop's wire shape is inspectable."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []

    def complete(self, messages, tools=()):
        self.requests.append({"messages": list(messages), "tools": list(tools)})
        return self._responses.pop(0)


def _ask_model(doc_id: str) -> _FakeChatModel:
    return _FakeChatModel(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "search_pdfs", "arguments": '{"query": "alpine"}'},
                    }
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "read_pdf_page",
                            "arguments": json.dumps({"doc_id": doc_id, "page_number": 9}),
                        },
                    }
                ],
            },
            {
                "role": "assistant",
                "content": f"Alpine meteorology is reported [{doc_id} p.9].",
            },
        ]
    )


def test_the_ask_flags_reach_the_command() -> None:
    args = build_parser().parse_args(
        [
            "ask",
            "--question",
            "Where is alpine meteorology?",
            "--base-url",
            "http://[::1]:11434/v1",
            "--model",
            "qwen3:1.7b",
            "--out",
            "o",
            "--top-k",
            "2",
            "--max-tool-calls",
            "4",
            "--json",
        ]
    )

    assert args.command == "ask"
    assert args.question.startswith("Where")
    assert args.base_url == "http://[::1]:11434/v1"
    assert args.model == "qwen3:1.7b"
    assert (args.out.name, args.top_k, args.max_tool_calls, args.json) == ("o", 2, 4, True)
    assert args.index is None


def test_ask_refuses_a_missing_index_without_calling_a_model(tmp_path, capsys, monkeypatch) -> None:
    from finepdfs.interfaces import cli

    def must_not_build(*args, **kwargs):
        raise AssertionError("ask must not build a model client without an index")

    monkeypatch.setattr(cli, "ChatCompletionsClient", must_not_build)
    args = build_parser().parse_args(
        [
            "ask",
            "--question",
            "q",
            "--base-url",
            "http://[::1]:11434/v1",
            "--model",
            "m",
            "--out",
            str(tmp_path / "out"),
        ]
    )

    assert cli._ask(args) == 2
    assert "no search index" in capsys.readouterr().err


def test_ask_refuses_a_non_positive_top_k(tmp_path, capsys, monkeypatch) -> None:
    from finepdfs.interfaces import cli

    out_dir = tmp_path / "out"
    _write_declared_chunks(out_dir)
    index = tmp_path / "search.db"
    assert cli.main(["index-search", "--out", str(out_dir), "--index", str(index)]) == 0
    capsys.readouterr()
    args = build_parser().parse_args(
        [
            "ask", "--question", "q", "--base-url", "http://[::1]:11434/v1", "--model", "m",
            "--out", str(out_dir), "--index", str(index), "--top-k", "0",
        ]
    )

    assert cli._ask(args) == 2
    assert "--top-k must be at least 1" in capsys.readouterr().err


def test_ask_prints_visible_tool_calls_and_citations(tmp_path, capsys, monkeypatch) -> None:
    from finepdfs.infrastructure.corpus.index import document_key
    from finepdfs.interfaces import cli

    out_dir = tmp_path / "out"
    _write_declared_chunks(out_dir)
    index = tmp_path / "search.db"
    assert cli.main(["index-search", "--out", str(out_dir), "--index", str(index)]) == 0
    capsys.readouterr()
    doc_id = document_key("https://example.org/page-9.pdf")
    model = _ask_model(doc_id)
    monkeypatch.setattr(cli, "ChatCompletionsClient", lambda *args, **kwargs: model)
    args = build_parser().parse_args(
        [
            "ask",
            "--question",
            "Where is alpine meteorology?",
            "--base-url",
            "http://[::1]:11434/v1",
            "--model",
            "qwen3:1.7b",
            "--out",
            str(out_dir),
            "--index",
            str(index),
        ]
    )
    args.api_key = "sk-this-must-not-be-printed"

    assert cli._ask(args) == 0

    printed = capsys.readouterr()
    assert "[tool 1] search_pdfs" in printed.out
    assert "-> 1 hits" in printed.out
    assert "[tool 2] read_pdf_page" in printed.out
    assert "-> accepted page 9" in printed.out
    assert f"- {doc_id} p.9" in printed.out
    assert "https://example.org/page-9.pdf" in printed.out
    assert "(page read)" in printed.out
    assert "Tool calls: 2 of 6" in printed.out
    assert "sk-this-must-not-be-printed" not in printed.out + printed.err
    # The tool result round trip reached the model, and the schemas were offered on the first turn.
    assert model.requests[0]["tools"]
    assert [message["role"] for message in model.requests[1]["messages"]][-1] == "tool"


def test_ask_json_carries_the_answer_citations_and_trace(tmp_path, capsys, monkeypatch) -> None:
    from finepdfs.infrastructure.corpus.index import document_key
    from finepdfs.interfaces import cli

    out_dir = tmp_path / "out"
    _write_declared_chunks(out_dir)
    index = tmp_path / "search.db"
    assert cli.main(["index-search", "--out", str(out_dir), "--index", str(index)]) == 0
    capsys.readouterr()
    doc_id = document_key("https://example.org/page-9.pdf")
    monkeypatch.setattr(cli, "ChatCompletionsClient", lambda *args, **kwargs: _ask_model(doc_id))
    args = build_parser().parse_args(
        [
            "ask",
            "--question",
            "Where is alpine meteorology?",
            "--base-url",
            "http://[::1]:11434/v1",
            "--model",
            "qwen3:1.7b",
            "--out",
            str(out_dir),
            "--index",
            str(index),
            "--json",
        ]
    )

    assert cli._ask(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["answer"].startswith("Alpine meteorology")
    assert payload["citations"][0]["doc_id"] == doc_id
    assert payload["citations"][0]["page_number"] == 9
    assert payload["citations"][0]["verified"] is True
    assert [call["tool"] for call in payload["tool_calls"]] == ["search_pdfs", "read_pdf_page"]
    assert payload["insufficient_evidence"] is False


NEAR_KEEPER_URL = "https://a.example/promoted.pdf"
DIGEST_KEEPER_URL = "https://b.example/digest-keeper.pdf"
DIGEST_DUPLICATE_URL = "https://c.example/digest-keeper.pdf"


def _near_loser_corpus(tmp_path: Path) -> Path:
    """Two digests: one shared by two URLs, one whose text is near-identical to it."""
    import hashlib

    from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path

    body = (Path(__file__).resolve().parent.parent / "fixtures" / "warc_payload.bin").read_bytes()
    contents = {
        NEAR_KEEPER_URL: body + b"\n% a different Document whose text is near-identical\n",
        DIGEST_KEEPER_URL: body,
        DIGEST_DUPLICATE_URL: body,
    }
    corpus_dir = tmp_path / "corpus"
    (corpus_dir / "pdfs").mkdir(parents=True)
    entries = []
    for url, content in contents.items():
        digest = hashlib.sha256(content).hexdigest()
        (corpus_dir / relative_pdf_path(digest)).write_bytes(content)
        entries.append(
            CorpusEntry(
                url=url,
                digest=digest,
                file=relative_pdf_path(digest),
                bytes=len(content),
                crawl_id="CC-MAIN-2026-39",
                warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
                offset=1,
                length=len(content),
            )
        )
    CorpusManifest(crawl_id="CC-MAIN-2026-39", domain="a.example", entries=entries).save(
        corpus_dir / "manifest.json"
    )
    return corpus_dir


def test_the_dry_run_cannot_lie_about_an_exact_duplicate_whose_digest_keeper_is_a_near_loser(
    tmp_path, monkeypatch, capsys
) -> None:
    """The dry run and the flow read one decision, so both name the same published near keeper.

    Before the shared decision the dry run printed the raw digest keeper as an exact duplicate and
    counted the Document as byte-identical, which the flow never declared: the dry run could lie about
    what a run would publish.
    """
    from finepdfs.interfaces import cli

    corpus_dir = _near_loser_corpus(tmp_path)
    out_dir = tmp_path / "out"
    monkeypatch.setenv("FINEPDFS_DB", str(tmp_path / "runs.db"))
    assert cli.main(["run", "--corpus", str(corpus_dir), "--out", str(out_dir), "--engine", "pymupdf"]) == 0

    declared = [json.loads(line) for line in (out_dir / "dedupe" / "duplicates.jsonl").read_text().splitlines()]
    published = {json.loads(path.read_text())["url"] for path in (out_dir / "records").glob("*.json")}
    assert published == {NEAR_KEEPER_URL}
    assert {row["duplicate_of"] for row in declared} == {NEAR_KEEPER_URL}

    capsys.readouterr()
    assert cli.main(["dry-run", "--corpus", str(corpus_dir), "--engine", "pymupdf"]) == 0

    printed = capsys.readouterr().out
    for row in declared:
        line = next(line for line in printed.splitlines() if row["url"] in line)
        reason = line.split(row["url"], 1)[1]
        assert line.split()[0] == f"{row['kind']}-duplicate", "the dry run names the kind the flow declared"
        assert NEAR_KEEPER_URL in reason, "the dry run names the keeper the flow declared"
        assert DIGEST_KEEPER_URL not in reason, "no row names the unpublished digest keeper"
    assert "0 byte-identical" in printed, "an exact duplicate of a near loser is not counted as exact"
