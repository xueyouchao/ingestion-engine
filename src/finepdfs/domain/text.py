"""Measuring and cleaning page text. Pure functions and rules, no I/O."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from finepdfs.domain.document import PageText

_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_PAGE_NUMBER_LINE = re.compile(r"^[\s\-—–]*\d{1,4}[\s\-—–]*$")  # noqa: RUF001 - the dashes are the point
_WHITESPACE_RUN = re.compile(r"[ \t]+")
_BLANK_LINE_RUN = re.compile(r"\n{3,}")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def garbled_ratio(text: str) -> float:
    """Share of characters that are unassigned, private-use, replacement or control characters."""
    if not text:
        return 0.0
    garbled = 0
    for character in text:
        if character in "\n\r\t":
            continue
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Co", "Cn"} or character == "\ufffd":
            garbled += 1
    return garbled / len(text)


def normalise(text: str) -> str:
    """Collapse whitespace and blank lines without touching wording."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RUN.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINE_RUN.sub("\n\n", text).strip()


@dataclass(frozen=True, slots=True)
class CleaningRules:
    """How one branch of the pipeline cleans its pages.

    Native text and OCR text fail differently: OCR invents page numbers, repeats table headers and
    breaks words across lines, while native text mostly suffers from running titles and layout
    whitespace. The rules are data so they can be asserted on directly.
    """

    repair_hyphenation: bool = True
    drop_running_titles: bool = True
    running_title_page_share: float = 0.6
    drop_page_number_lines: bool = True
    drop_lines_repeated_within_page: bool = False
    drop_letterless_lines: bool = False

    @classmethod
    def for_native_text(cls) -> CleaningRules:
        return cls(repair_hyphenation=False, drop_lines_repeated_within_page=False, drop_letterless_lines=False)

    @classmethod
    def for_ocr_text(cls) -> CleaningRules:
        return cls(repair_hyphenation=True, drop_lines_repeated_within_page=True, drop_letterless_lines=True)

    def clean(self, pages: Sequence[PageText]) -> tuple[PageText, ...]:
        """Normalise every page, then remove what the rules call noise."""
        prepared = [PageText(number=page.number, text=normalise(page.text)) for page in pages]
        if self.repair_hyphenation:
            prepared = [PageText(number=page.number, text=_HYPHEN_BREAK.sub(r"\1\2", page.text)) for page in prepared]
        if self.drop_page_number_lines:
            prepared = [PageText(number=page.number, text=_drop_lines(page.text, _is_page_number)) for page in prepared]
        if self.drop_letterless_lines:
            prepared = [PageText(number=page.number, text=_drop_lines(page.text, _has_no_letters)) for page in prepared]
        if self.drop_lines_repeated_within_page:
            prepared = [PageText(number=page.number, text=_drop_repeats(page.text)) for page in prepared]
        if self.drop_running_titles:
            prepared = _drop_running_titles(prepared, self.running_title_page_share)
        return tuple(page for page in prepared if page.text.strip())


def _is_page_number(line: str) -> bool:
    return bool(_PAGE_NUMBER_LINE.match(line))


def _has_no_letters(line: str) -> bool:
    return not _LETTER.search(line)


def _drop_lines(text: str, unwanted) -> str:
    return "\n".join(line for line in text.split("\n") if not unwanted(line))


def _drop_repeats(text: str) -> str:
    lines = text.split("\n")
    seen: set[str] = set()
    kept = []
    for line in lines:
        stripped = line.strip()
        if stripped and stripped in seen:
            continue
        if stripped:
            seen.add(stripped)
        kept.append(line)
    return "\n".join(kept)


def _drop_running_titles(pages: Sequence[PageText], page_share: float) -> list[PageText]:
    """Remove lines that appear on most pages: headers, footers and running titles."""
    if len(pages) < 3:
        return list(pages)
    counts: dict[str, int] = {}
    for page in pages:
        for line in {line.strip() for line in page.text.split("\n") if line.strip()}:
            counts[line] = counts.get(line, 0) + 1
    threshold = max(2, int(len(pages) * page_share))
    repeated = {line for line, count in counts.items() if count >= threshold}
    if not repeated:
        return list(pages)
    def keep(line: str) -> bool:
        return line.strip() not in repeated

    return [
        PageText(number=page.number, text="\n".join(line for line in page.text.split("\n") if keep(line)))
        for page in pages
    ]
