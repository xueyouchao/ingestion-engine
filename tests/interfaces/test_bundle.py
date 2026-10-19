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
