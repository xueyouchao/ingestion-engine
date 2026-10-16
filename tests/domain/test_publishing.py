"""The publish policy: the minimum a document must be."""

from __future__ import annotations

from finepdfs.domain.document import PageText
from finepdfs.domain.language import LanguageOutcome
from finepdfs.domain.publishing import PublishPolicy


def _pages(text: str, count: int = 1) -> list[PageText]:
    return [PageText(number=index, text=text) for index in range(1, count + 1)]


def _language(language: str = "eng") -> LanguageOutcome:
    return LanguageOutcome(language=language, accepted_pages=(1,), dropped_pages=(), confidence=0.9)


def test_a_long_confident_document_is_published() -> None:
    published, reason = PublishPolicy().judge(_pages("word " * 200, count=3), _language())

    assert published is True
    assert reason == "published"


def test_a_short_document_is_dropped_with_the_character_count_in_the_reason() -> None:
    published, reason = PublishPolicy(min_characters=200).judge(_pages("too short"), _language())

    assert published is False
    assert "fewer than the 200 character minimum" in reason


def test_an_unknown_language_is_dropped() -> None:
    published, reason = PublishPolicy().judge(_pages("word " * 100), _language("unknown"))

    assert published is False
    assert "language confident enough" in reason


def test_a_language_outside_the_allowed_set_is_dropped() -> None:
    published, reason = PublishPolicy(required_languages=("eng", "deu")).judge(_pages("word " * 100), _language("rus"))

    assert published is False
    assert "not one of the required languages (eng, deu)" in reason
