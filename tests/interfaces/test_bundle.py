"""The bundle: a derived static export of what the flow declared.

The fixture writes the same files a completed run declares -- summary, records, pages, chunks and
exclusions -- beside a corpus holding the PDF those records name. Nothing here runs the pipeline: these
tests prove the exporter reads declared outputs and nothing else, shares page renders between engines,
fingerprints what it mirrored, refuses what it cannot read, and never writes into a state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from finepdfs.infrastructure.corpus.index import document_key
from finepdfs.interfaces.bundle import BundleError, build_bundle, check_bundle, read_state

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
URL = "https://a.example/wp-content/uploads/2025/03/Resolution-XXVI-6.pdf"
KEY = document_key(URL)
TEXT = {
    1: "Resolution XXVI-6 on data buoy vandalism.",
    2: "Acknowledging that member states act.",
    3: "Encourage binding measures.",
}


def _body() -> bytes:
    return (FIXTURES / "warc_payload.bin").read_bytes()


def _write_corpus(tmp_path: Path) -> str:
    body = _body()
    digest = hashlib.sha256(body).hexdigest()
    (tmp_path / "corpus" / "pdfs").mkdir(parents=True)
    (tmp_path / "corpus" / "pdfs" / f"{digest}.pdf").write_bytes(body)
    return digest


def _write_run(
    tmp_path: Path,
    name: str,
    *,
    pages: tuple[int, ...],
    engine: str,
    excluded: tuple[dict[str, str], ...] = (),
) -> Path:
    digest = _write_corpus(tmp_path) if not (tmp_path / "corpus").is_dir() else _corpus_digest(tmp_path)
    out = tmp_path / name
    for directory in ("records", "pages", "chunks", "excluded"):
        (out / directory).mkdir(parents=True, exist_ok=True)
    summary = {
        "corpus": {"crawl_records": 1, "documents": 1},
        "routing": {"documents": 1, "native": 1, "ocr": 0},
        "extraction": {"requested": 1, "calls": 0, "reused": 1},
        "published": {"documents": 1, "page_records": len(pages)},
        "exclusions": {"total": len(excluded), "by_kind": {}},
        "engine": {"name": engine.split("@")[0], "identity": engine},
    }
    (out / "run-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (out / "records" / f"{KEY}.json").write_text(
        json.dumps(
            {
                "url": URL,
                "digest": digest,
                "engine": engine,
                "routing_decision": "text-layer",
                "routing_reason": "the text layer is healthy",
                "language": "Latn",
                "page_count": len(pages),
                "accepted_pages": len(pages),
                "dropped_pages": 0,
                "character_count": sum(len(TEXT[number]) for number in pages),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "pages" / f"{KEY}.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "doc_id": KEY,
                    "source_url": URL,
                    "digest": digest,
                    "page_number": number,
                    "text": TEXT[number],
                    "engine": engine,
                }
            )
            for number in pages
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "chunks" / f"{KEY}.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "chunk_id": f"{KEY}-{number}",
                    "chunking_version": "chunking-v1",
                    "digest": digest,
                    "doc_id": KEY,
                    "engine": engine,
                    "language": "Latn",
                    "ordinal": 0,
                    "page_number": number,
                    "source_url": URL,
                    "start_offset": 0,
                    "end_offset": len(TEXT[number]),
                    "text": TEXT[number],
                }
            )
            for number in pages
        )
        + "\n",
        encoding="utf-8",
    )
    for index, row in enumerate(excluded):
        (out / "excluded" / f"{index}.json").write_text(json.dumps(row) + "\n", encoding="utf-8")
    return out


def _corpus_digest(tmp_path: Path) -> str:
    return hashlib.sha256(_body()).hexdigest()


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_the_default_bundle_keeps_its_renders_in_an_images_directory(tmp_path) -> None:
    out = _write_run(tmp_path, "out", pages=(1, 2, 3), engine="pymupdf@1.28.2")
    bundle = tmp_path / "bundle"

    contract = build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus")

    assert (bundle / "index.html").is_file()
    assert (bundle / "bundle.json").is_file()
    assert len(list((bundle / "images").glob("*.jpg"))) == 3
    assert contract["mode"] == "split"
    assert len(contract["images"]) == 3
    page = (bundle / "index.html").read_text(encoding="utf-8")
    assert "const DATA = {" in page
    assert "data:image/jpeg;base64," not in page
    assert "Resolution XXVI-6 on data buoy vandalism." in page


def test_an_inline_bundle_is_one_self_contained_file(tmp_path) -> None:
    out = _write_run(tmp_path, "out", pages=(1, 2, 3), engine="pymupdf@1.28.2")
    bundle = tmp_path / "bundle"

    contract = build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus", inline=True)

    assert not (bundle / "images").exists()
    assert contract["mode"] == "inline"
    assert contract["total_bytes"] == (bundle / "index.html").stat().st_size
    assert (bundle / "index.html").read_text(encoding="utf-8").count("data:image/jpeg;base64,") == 3


def test_the_contract_records_the_schema_and_what_it_mirrored(tmp_path) -> None:
    out = _write_run(tmp_path, "out", pages=(1, 2), engine="pymupdf@1.28.2")
    bundle = tmp_path / "bundle"

    contract = build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus")

    assert contract["schema_version"] == 1
    assert contract["default_state"] == "pymupdf"
    assert contract["dpi"] == 72
    assert len(contract["states"][0]["declared_fingerprint"]) == 64
    written = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    assert written == contract


def test_a_second_state_shares_the_renders_and_keeps_its_own_reading(tmp_path) -> None:
    first = _write_run(tmp_path, "gpu", pages=(1, 2, 3), engine="paddleocr-vl@x")
    second = _write_run(tmp_path, "cpu", pages=(1, 2), engine="pymupdf@1.28.2")
    bundle = tmp_path / "bundle"

    contract = build_bundle(
        bundle,
        [read_state("gpu", first), read_state("cpu", second)],
        tmp_path / "corpus",
    )

    assert contract["pages"] == 3
    assert len(list((bundle / "images").glob("*.jpg"))) == 3
    assert [state["label"] for state in contract["states"]] == ["gpu", "cpu"]
    page = (bundle / "index.html").read_text(encoding="utf-8")
    assert '"readings":{"gpu"' in page and '"cpu"' in page


def test_an_exclusion_is_carried_so_the_reader_can_explain_a_missing_page(tmp_path) -> None:
    first = _write_run(tmp_path, "gpu", pages=(1, 2), engine="paddleocr-vl@x")
    second = _write_run(
        tmp_path,
        "cpu",
        pages=(1,),
        engine="pymupdf@1.28.2",
        excluded=({"url": URL, "kind": "dropped", "reason": "0 pages is fewer than the 1 page minimum"},),
    )
    bundle = tmp_path / "bundle"

    build_bundle(bundle, [read_state("gpu", first), read_state("cpu", second)], tmp_path / "corpus")

    page = (bundle / "index.html").read_text(encoding="utf-8")
    assert "0 pages is fewer than the 1 page minimum" in page


def test_check_reports_a_state_whose_declared_evidence_changed(tmp_path) -> None:
    out = _write_run(tmp_path, "out", pages=(1, 2), engine="pymupdf@1.28.2")
    runs = [read_state("pymupdf", out)]
    build_bundle(tmp_path / "bundle", runs, tmp_path / "corpus")
    assert check_bundle(tmp_path / "bundle", runs) == []

    chunk_file = out / "chunks" / f"{KEY}.jsonl"
    chunk_file.write_text(chunk_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert check_bundle(tmp_path / "bundle", [read_state("pymupdf", out)]) == ["pymupdf"]


def test_a_state_without_a_summary_is_refused(tmp_path) -> None:
    out = _write_run(tmp_path, "out", pages=(1,), engine="pymupdf@1.28.2")
    (out / "run-summary.json").unlink()

    with pytest.raises(BundleError, match=r"run-summary\.json"):
        read_state("pymupdf", out)


def test_the_budget_refuses_rather_than_shrinking(tmp_path) -> None:
    out = _write_run(tmp_path, "out", pages=(1, 2), engine="pymupdf@1.28.2")

    with pytest.raises(BundleError, match="ceiling"):
        build_bundle(tmp_path / "bundle", [read_state("pymupdf", out)], tmp_path / "corpus", max_bytes=1024)


def test_bundling_writes_nothing_into_a_state(tmp_path) -> None:
    out = _write_run(tmp_path, "out", pages=(1, 2), engine="pymupdf@1.28.2")
    before = _snapshot(out)

    build_bundle(tmp_path / "bundle", [read_state("pymupdf", out)], tmp_path / "corpus")

    assert _snapshot(out) == before


def _data(bundle: Path) -> dict:
    page = (bundle / "index.html").read_text(encoding="utf-8")
    start = page.index("const DATA = ") + len("const DATA = ")
    return json.loads(page[start:page.index(";\n", start)])


def test_a_refused_rebuild_leaves_the_served_bundle_alone(tmp_path) -> None:
    """The served directory is live: an over-budget rebuild must not publish half of itself."""
    out = _write_run(tmp_path, "out", pages=(1, 2), engine="pymupdf@1.28.2")
    runs = [read_state("pymupdf", out)]
    bundle = tmp_path / "bundle"
    build_bundle(bundle, runs, tmp_path / "corpus")
    before = _snapshot(bundle)

    with pytest.raises(BundleError, match="ceiling"):
        build_bundle(bundle, runs, tmp_path / "corpus", max_bytes=1024)

    assert _snapshot(bundle) == before


def test_the_fingerprint_covers_records_pages_and_exclusions(tmp_path) -> None:
    out = _write_run(
        tmp_path, "out", pages=(1,), engine="pymupdf@1.28.2",
        excluded=({"url": "https://a.example/other.pdf", "kind": "dropped", "reason": "0 pages"},),
    )
    bundle = tmp_path / "bundle"
    build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus")
    assert check_bundle(bundle, [read_state("pymupdf", out)]) == []

    excluded_file = next((out / "excluded").glob("*.json"))
    excluded_file.write_text(
        json.dumps({"url": "https://a.example/other.pdf", "kind": "dropped", "reason": "a different reason"})
        + "\n",
        encoding="utf-8",
    )

    assert check_bundle(bundle, [read_state("pymupdf", out)]) == ["pymupdf"]


def test_two_states_cannot_share_a_label(tmp_path) -> None:
    out = _write_run(tmp_path, "out", pages=(1,), engine="pymupdf@1.28.2")

    with pytest.raises(BundleError, match="share the label"):
        build_bundle(tmp_path / "bundle", [read_state("cpu", out), read_state("cpu", out)], tmp_path / "corpus")


def test_a_state_that_published_nothing_sits_beside_one_that_did(tmp_path) -> None:
    published = _write_run(tmp_path, "gpu", pages=(1, 2), engine="paddleocr-vl@x")
    empty = _write_run(
        tmp_path, "cpu", pages=(), engine="pymupdf@1.28.2",
        excluded=({"url": URL, "kind": "dropped", "reason": "0 pages is fewer than the 1 page minimum"},),
    )
    runs = [read_state("gpu", published), read_state("cpu", empty)]

    contract = build_bundle(tmp_path / "bundle", runs, tmp_path / "corpus")

    assert contract["pages"] == 2
    assert [state["pages"] for state in contract["states"]] == [2, 0]
    assert [state["exclusions"] for state in contract["states"]] == [0, 1]

    with pytest.raises(BundleError, match="no supplied state publishes a page"):
        build_bundle(tmp_path / "nothing", [read_state("cpu", empty)], tmp_path / "corpus")


def test_a_rebuild_removes_the_renders_it_no_longer_references(tmp_path) -> None:
    out = _write_run(tmp_path, "out", pages=(1, 2, 3), engine="pymupdf@1.28.2")
    bundle = tmp_path / "bundle"
    build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus")
    assert len(list((bundle / "images").glob("*.jpg"))) == 3

    out = _write_run(tmp_path, "out", pages=(1,), engine="pymupdf@1.28.2")
    build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus")

    assert len(list((bundle / "images").glob("*.jpg"))) == 1


def test_a_render_name_carries_its_own_bytes(tmp_path) -> None:
    """The page is cached immutable, so another dpi has to be another URL."""
    out = _write_run(tmp_path, "out", pages=(1,), engine="pymupdf@1.28.2")
    runs = [read_state("pymupdf", out)]

    first = build_bundle(tmp_path / "bundle", runs, tmp_path / "corpus")["images"][0]["path"]
    second = build_bundle(tmp_path / "bundle", runs, tmp_path / "corpus", dpi=144)["images"][0]["path"]

    assert first != second
    assert len(list((tmp_path / "bundle" / "images").glob("*.jpg"))) == 1


def test_states_reading_different_pdfs_for_one_url_are_refused(tmp_path) -> None:
    gpu = _write_run(tmp_path, "gpu", pages=(1,), engine="paddleocr-vl@x")
    cpu = _write_run(tmp_path, "cpu", pages=(1,), engine="pymupdf@1.28.2")
    digest = _corpus_digest(tmp_path)
    other = hashlib.sha256(_body() + b"\n% a different fetch of the same URL\n").hexdigest()
    (tmp_path / "corpus" / "pdfs" / f"{other}.pdf").write_bytes(
        _body() + b"\n% a different fetch of the same URL\n"
    )
    for path in sorted(cpu.rglob("*")):
        if path.is_file():
            path.write_text(path.read_text(encoding="utf-8").replace(digest, other), encoding="utf-8")

    with pytest.raises(BundleError, match="different PDFs for the same source URL"):
        build_bundle(
            tmp_path / "bundle", [read_state("gpu", gpu), read_state("cpu", cpu)], tmp_path / "corpus"
        )


def test_chunk_offsets_are_what_javascript_counts(tmp_path) -> None:
    """Declared offsets count Python characters; `String.prototype.slice` counts UTF-16 units."""
    out = _write_run(tmp_path, "out", pages=(1,), engine="pymupdf@1.28.2")
    text = "\U0001d518nicode ordering. Second sentence."
    start = text.index("Second")
    (out / "pages" / f"{KEY}.jsonl").write_text(
        json.dumps({"doc_id": KEY, "source_url": URL, "digest": _corpus_digest(tmp_path), "page_number": 1,
                    "text": text, "engine": "pymupdf@1.28.2"}) + "\n",
        encoding="utf-8",
    )
    chunks = json.loads((out / "chunks" / f"{KEY}.jsonl").read_text(encoding="utf-8").strip())
    chunks["start_offset"], chunks["end_offset"] = start, len(text)
    (out / "chunks" / f"{KEY}.jsonl").write_text(json.dumps(chunks) + "\n", encoding="utf-8")

    bundle = tmp_path / "bundle"
    build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus")

    reading = _data(bundle)["pages"][f"{KEY}:1"]["readings"]["pymupdf"]
    units = reading["text"].encode("utf-16-le")
    chunk = reading["chunks"][0]
    # The astral character before the chunk is two UTF-16 units, so a JavaScript slice from these
    # offsets still lands on "Second"; the declared Python offsets would have been one short.
    assert chunk["start"] == start + 1
    assert chunk["end"] == len(units) // 2
    assert units[chunk["start"] * 2:chunk["end"] * 2].decode("utf-16-le") == "Second sentence."


def test_a_rebuild_retires_renders_named_before_names_carried_a_hash(tmp_path) -> None:
    """The name changed once already; the older shape is still this exporter's to delete."""
    out = _write_run(tmp_path, "out", pages=(1, 2), engine="pymupdf@1.28.2")
    bundle = tmp_path / "bundle"
    build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus")
    legacy = bundle / "images" / f"{_corpus_digest(tmp_path)}-p9.jpg"
    legacy.write_bytes(b"a render from the previous naming")

    build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus")

    assert not legacy.exists()
    assert len(list((bundle / "images").glob("*.jpg"))) == 2


def test_the_fingerprint_ignores_files_the_bundle_never_reads(tmp_path) -> None:
    """A stray file in a declared directory is not evidence the page shows."""
    out = _write_run(tmp_path, "out", pages=(1,), engine="pymupdf@1.28.2")
    bundle = tmp_path / "bundle"
    build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus")

    (out / "pages" / "a-scratch-note.txt").write_text("not declared evidence\n", encoding="utf-8")

    assert check_bundle(bundle, [read_state("pymupdf", out)]) == []


def test_a_contract_from_another_schema_is_not_checked(tmp_path) -> None:
    out = _write_run(tmp_path, "out", pages=(1,), engine="pymupdf@1.28.2")
    bundle = tmp_path / "bundle"
    build_bundle(bundle, [read_state("pymupdf", out)], tmp_path / "corpus")
    contract = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    contract["schema_version"] = 99
    (bundle / "bundle.json").write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(BundleError, match="schema version"):
        check_bundle(bundle, [read_state("pymupdf", out)])
