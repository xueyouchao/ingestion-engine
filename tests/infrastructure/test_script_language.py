"""The dependency-free language identifier, and what it refuses to claim."""

from __future__ import annotations

from finepdfs.domain.language import UNKNOWN_LANGUAGE
from finepdfs.infrastructure.language.script import ScriptLanguageIdentifier

ENGLISH = "This resolution concerns the vandalism of weather instruments in remote stations, and it " * 2
GREEK = "Η απόφαση αφορά τον βανδαλισμό των μετεωρολογικών οργάνων σε απομακρυσμένους σταθμούς " * 2


def test_latin_text_is_identified_as_latin_script_with_high_confidence() -> None:
    tag = ScriptLanguageIdentifier().identify(ENGLISH, page_number=1)

    assert tag.language == "Latn"
    assert tag.confidence > 0.9


def test_greek_text_is_identified_as_greek_script() -> None:
    assert ScriptLanguageIdentifier().identify(GREEK, page_number=2).language == "Grek"


def test_a_page_with_too_little_text_is_unknown_not_a_guess() -> None:
    tag = ScriptLanguageIdentifier().identify("3", page_number=5)

    assert tag.language == UNKNOWN_LANGUAGE
    assert tag.confidence == 0.0


def test_mixed_script_text_reports_the_dominant_script() -> None:
    mixed = ENGLISH * 3 + "αβγδεζηθικλμνξοπρστυφχψω " * 4

    tag = ScriptLanguageIdentifier().identify(mixed, page_number=1)

    assert tag.language == "Latn"
    assert 0.5 < tag.confidence < 1.0


def test_a_tie_between_scripts_is_unknown_not_an_order_dependent_winner() -> None:
    """A tie has no winner; ``most_common(1)`` used to pick whichever script appeared first."""
    identifier = ScriptLanguageIdentifier()

    for text in ("α" * 25 + "a" * 25, "a" * 25 + "α" * 25):
        tag = identifier.identify(text, page_number=1)

        assert tag.language == UNKNOWN_LANGUAGE
        assert tag.confidence == 0.0
