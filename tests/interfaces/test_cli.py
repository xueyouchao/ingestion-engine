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

    assert cli.main(["search", "--index", str(index), "harbour"]) == 0
    printed = capsys.readouterr().out
    assert "p.4" in printed
    assert "https://example.org/page-4.pdf" in printed
    assert "<mark>Harbour</mark>" in printed

    assert cli.main(["search", "--index", str(index), "--json", "harbour"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    hit = payload["hits"][0]
    assert set(hit) >= {"doc_id", "chunk_id", "source_url", "digest", "page_number", "text", "snippet", "score"}
    assert hit["page_number"] == 4

    assert cli.main(["search", "--index", str(index), "--language", "fra", "harbour"]) == 0
    assert "no matches" in capsys.readouterr().out


def test_indexing_a_directory_that_holds_no_run_is_an_operator_error(tmp_path, capsys) -> None:
    from finepdfs.interfaces import cli

    assert cli.main(["index-search", "--out", str(tmp_path / "nothing"), "--index", str(tmp_path / "i.db")]) == 2
    assert "no published output" in capsys.readouterr().err
