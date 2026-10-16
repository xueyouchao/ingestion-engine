"""Cleaning page text: what survives, what is removed, and why the branches differ."""

from __future__ import annotations

from finepdfs.domain.document import PageText
from finepdfs.domain.text import CleaningRules, garbled_ratio, normalise


def _pages(*texts: str) -> list[PageText]:
    return [PageText(number=index, text=text) for index, text in enumerate(texts, start=1)]


def test_normalise_collapses_layout_whitespace_without_rewriting_words() -> None:
    assert normalise("Hello    world\n\n\n\nnext   line  \n") == "Hello world\n\nnext line"


def test_garbled_ratio_counts_replacement_and_control_characters() -> None:
    assert garbled_ratio("clean text") == 0.0
    assert garbled_ratio("clean\ufffd\ufffd") > 0.2


def test_running_titles_are_removed_from_every_page() -> None:
    pages = _pages(
        "Annual Report 2026\nfirst body",
        "Annual Report 2026\nsecond body",
        "Annual Report 2026\nthird body",
    )

    cleaned = CleaningRules.for_native_text().clean(pages)

    assert [page.text for page in cleaned] == ["first body", "second body", "third body"]


def test_a_line_on_only_some_pages_is_kept() -> None:
    pages = _pages("Report\nfirst body", "second body", "third body", "fourth body")

    cleaned = CleaningRules.for_native_text().clean(pages)

    assert cleaned[0].text == "Report\nfirst body"


def test_both_branches_drop_page_numbers() -> None:
    pages = _pages("body text\n- 12 -", "body text\n13")

    assert [page.text for page in CleaningRules.for_native_text().clean(pages)] == ["body text", "body text"]
    assert [page.text for page in CleaningRules.for_ocr_text().clean(pages)] == ["body text", "body text"]


def test_ocr_cleaning_repairs_hyphenated_line_breaks_and_repeats() -> None:
    pages = _pages("a docu-\nment", "Table 3: results\nTable 3: results\nvalue")

    cleaned = CleaningRules.for_ocr_text().clean(pages)

    assert cleaned[0].text == "a document"
    assert cleaned[1].text == "Table 3: results\nvalue"


def test_ocr_cleaning_drops_lines_without_letters() -> None:
    cleaned = CleaningRules.for_ocr_text().clean(_pages("body text\n| | —\n1 2 3"))

    assert cleaned[0].text == "body text"


def test_pages_left_empty_are_dropped() -> None:
    cleaned = CleaningRules.for_ocr_text().clean(_pages("4", "", "real text"))

    assert [page.number for page in cleaned] == [3]
