"""Reading one accepted Page Text, never the Markdown and never a stale export.

The page read is the agent's second tool and the browser's evidence view, so it is tested through the
same declared outputs the flow writes: a published record names the current digest, and the page export
names the exact text of one original page. A page that is absent, excluded, duplicated, unpublished,
retracted or left behind by a replacement is an explicit result, not a guess and not a crash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finepdfs.application.page_reads import PageRead, read_pdf_page
from finepdfs.infrastructure.corpus.index import document_key
from finepdfs.infrastructure.publishing.disk import DiskPageReader

URL = "https://a.example/one.pdf"
DUPLICATE_URL = "https://a.example/duplicate.pdf"
DIGEST = "1" * 64
OTHER_DIGEST = "2" * 64


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_pages(path: Path, pages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(page) + "\n" for page in pages), encoding="utf-8")


def _accepted_output(tmp_path: Path) -> tuple[Path, str]:
    out = tmp_path / "out"
    key = document_key(URL)
    _write_json(
        out / "records" / f"{key}.json",
        {
            "url": URL,
            "digest": DIGEST,
            "engine": "pymupdf@1.28.2",
            "routing_decision": "text-layer",
            "routing_reason": "the text layer is healthy",
            "language": "eng",
            "page_count": 3,
            "accepted_pages": 2,
            "dropped_pages": 1,
            "character_count": 40,
        },
    )
    _write_pages(
        out / "pages" / f"{key}.jsonl",
        [
            {
                "doc_id": key,
                "source_url": URL,
                "digest": DIGEST,
                "page_number": 1,
                "text": "first accepted page text",
                "engine": "pymupdf@1.28.2",
            },
            {
                "doc_id": key,
                "source_url": URL,
                "digest": DIGEST,
                "page_number": 3,
                "text": "third accepted page text",
                "engine": "pymupdf@1.28.2",
            },
        ],
    )
    # The Markdown projection exists beside the pages. A page read must never fall back to it, so its
    # text is deliberately different from the accepted page text.
    (out / "docs").mkdir()
    (out / "docs" / f"{key}.md").write_text("Markdown projection, not a citation source\n", encoding="utf-8")
    duplicate_key = document_key(DUPLICATE_URL)
    _write_json(
        out / "excluded" / f"{duplicate_key}.json",
        {
            "url": DUPLICATE_URL,
            "key": duplicate_key,
            "kind": "exact-duplicate",
            "reason": f"identical bytes to {URL}",
        },
    )
    return out, key


def test_an_accepted_page_returns_the_exact_exported_text(tmp_path) -> None:
    out, key = _accepted_output(tmp_path)

    result = read_pdf_page(DiskPageReader(out), key, 3)

    assert isinstance(result, PageRead)
    assert result.status == "accepted"
    assert result.found
    assert result.page is not None
    assert result.page.text == "third accepted page text"
    assert result.page.page_number == 3
    assert result.page.doc_id == key
    assert result.page.source_url == URL
    assert result.page.digest == DIGEST
    assert result.page.engine == "pymupdf@1.28.2"
    assert result.accepted_pages == (1, 3)
    assert result.as_json()["citation"] == f"{key} p.3"


def test_a_page_that_is_not_accepted_is_not_found_and_names_the_accepted_pages(tmp_path) -> None:
    out, key = _accepted_output(tmp_path)

    result = read_pdf_page(DiskPageReader(out), key, 2)

    assert result.status == "not-found"
    assert not result.found
    assert result.page is None
    assert result.accepted_pages == (1, 3)
    assert "page 2 is not an accepted page" in result.reason


def test_markdown_is_never_reconstructed_as_page_text(tmp_path) -> None:
    out, key = _accepted_output(tmp_path)
    (out / "pages" / f"{key}.jsonl").unlink()
    (out / "records" / f"{key}.json").unlink()

    result = read_pdf_page(DiskPageReader(out), key, 1)

    assert result.status == "not-found"
    assert "Markdown projection" not in json.dumps(result.as_json())


def test_a_duplicate_is_unsupported_with_its_exclusion_reason(tmp_path) -> None:
    out, _ = _accepted_output(tmp_path)

    result = read_pdf_page(DiskPageReader(out), document_key(DUPLICATE_URL), 1)

    assert result.status == "unsupported"
    assert result.found is False
    assert result.page is None
    assert "exact-duplicate" in result.reason
    assert f"identical bytes to {URL}" in result.reason


def test_an_absent_document_is_not_found(tmp_path) -> None:
    out, _ = _accepted_output(tmp_path)

    result = read_pdf_page(DiskPageReader(out), "0" * 16, 1)

    assert result.status == "not-found"
    assert "not a published document" in result.reason


def test_a_page_export_left_behind_by_a_removal_is_unsupported(tmp_path) -> None:
    """Removing the record retracts the citation even if a page file survived the change."""
    out, key = _accepted_output(tmp_path)
    (out / "records" / f"{key}.json").unlink()

    result = read_pdf_page(DiskPageReader(out), key, 1)

    assert result.status == "unsupported"
    assert "no current published record" in result.reason
    assert result.page is None


def test_a_page_export_naming_an_old_digest_is_stale_evidence(tmp_path) -> None:
    """Replacement changes the record's digest; a page still naming the old bytes cannot be cited."""
    out, key = _accepted_output(tmp_path)
    record_path = out / "records" / f"{key}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["digest"] = OTHER_DIGEST
    _write_json(record_path, record)

    result = read_pdf_page(DiskPageReader(out), key, 1)

    assert result.status == "unsupported"
    assert "stale" in result.reason
    assert DIGEST in result.reason
    assert result.page is None


def test_the_reader_refuses_a_key_that_is_not_a_source_identity(tmp_path) -> None:
    out, _ = _accepted_output(tmp_path)

    with pytest.raises(ValueError, match="doc_id"):
        read_pdf_page(DiskPageReader(out), "../records", 1)
    with pytest.raises(ValueError, match="page_number"):
        read_pdf_page(DiskPageReader(out), "0" * 16, 0)


def test_a_missing_output_directory_is_a_clean_not_found(tmp_path) -> None:
    result = read_pdf_page(DiskPageReader(tmp_path / "missing"), "0" * 16, 1)

    assert result.status == "not-found"
    assert "no declared output" in result.reason
