"""The letter floor both language adapters apply before they try to name a page.

Below a few dozen letters neither a script count nor a trained model has anything to go on, so both
adapters reject the same page for the same reason and report the same unknown tag. Keeping the check
and the tag here stops the two from drifting on what "too little text" means.
"""

from __future__ import annotations

from finepdfs.domain.language import UNKNOWN_LANGUAGE, PageLanguage

MIN_CHARACTERS = 40


def enough_letters(text: str, minimum: int = MIN_CHARACTERS) -> bool:
    """Whether the page carries enough letters for a rule or a model to have something to go on."""
    return sum(character.isalpha() for character in text) >= minimum


def unknown_page(page_number: int) -> PageLanguage:
    """The tag for a page nothing could name; the LanguagePolicy drops it, the run does not fail."""
    return PageLanguage(page_number=page_number, language=UNKNOWN_LANGUAGE, confidence=0.0)
