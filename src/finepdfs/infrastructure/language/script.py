"""A dependency-free language identifier: it names the script, not the language.

Latn covers English and German alike, so this adapter cannot tell them apart. It is honest about that:
the confidence is the share of characters in the winning script, and a page whose largest scripts are
tied is reported as unknown. A fastText or GlotLID adapter can replace it behind the same port when
real language naming is needed. See ADR-0001.
"""

from __future__ import annotations

from collections import Counter

from finepdfs.domain.language import UNKNOWN_LANGUAGE, PageLanguage
from finepdfs.infrastructure.language.floor import MIN_CHARACTERS, enough_letters, unknown_page

SCRIPT_RANGES: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    ("Latn", ((0x0041, 0x024F), (0x1E00, 0x1EFF), (0x2C60, 0x2C7F), (0xA720, 0xA7FF))),
    ("Cyrl", ((0x0400, 0x052F), (0x2DE0, 0x2DFF), (0xA640, 0xA69F))),
    ("Grek", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),
    ("Arab", ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    ("Hebr", ((0x0590, 0x05FF), (0xFB1D, 0xFB4F))),
    ("Deva", ((0x0900, 0x097F), (0xA8E0, 0xA8FF))),
    ("Hang", ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))),
    ("Kana", ((0x3040, 0x30FF),)),
    ("Hani", ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))),
    ("Thai", ((0x0E00, 0x0E7F),)),
)


class ScriptLanguageIdentifier:
    """Names the dominant script of a page and how dominant it is."""

    def __init__(self, min_characters: int = MIN_CHARACTERS) -> None:
        self._min_characters = min_characters

    def identify(self, text: str, page_number: int) -> PageLanguage:
        if not enough_letters(text, self._min_characters):
            return unknown_page(page_number)

        letters = [character for character in text if character.isalpha()]
        counts: Counter[str] = Counter(_script_of(character) for character in letters)
        counts.pop(UNKNOWN_LANGUAGE, None)
        if not counts:
            return unknown_page(page_number)

        ranked = counts.most_common()
        # A tie for the largest share has no winner: most_common(1) would return whichever script the
        # text happened to mention first, making the tag depend on character order rather than on the
        # page. The adapter's promise is that it does not guess, so an ambiguous page is unknown.
        if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
            return unknown_page(page_number)
        script, count = ranked[0]
        return PageLanguage(page_number=page_number, language=script, confidence=count / len(letters))


def _script_of(character: str) -> str:
    code_point = ord(character)
    for script, ranges in SCRIPT_RANGES:
        for start, end in ranges:
            if start <= code_point <= end:
                return script
    return UNKNOWN_LANGUAGE
