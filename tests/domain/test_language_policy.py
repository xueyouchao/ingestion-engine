"""Choosing a document language from per-page tags."""

from __future__ import annotations

import pytest

from finepdfs.domain.language import LanguagePolicy, PageLanguage


def test_the_majority_trusted_language_wins() -> None:
    tags = [
        PageLanguage(1, "eng", 0.9),
        PageLanguage(2, "eng", 0.8),
        PageLanguage(3, "deu", 0.95),
    ]

    outcome = LanguagePolicy().apply(tags, page_count=3)

    assert outcome.language == "eng"
    assert outcome.accepted_pages == (1, 2)
    assert outcome.dropped_pages == (3,)
    assert outcome.confidence == pytest.approx(0.85)


def test_low_confidence_tags_are_dropped() -> None:
    tags = [PageLanguage(1, "eng", 0.4), PageLanguage(2, "deu", 0.9)]

    outcome = LanguagePolicy(min_confidence=0.65).apply(tags, page_count=2)

    assert outcome.language == "deu"
    assert outcome.accepted_pages == (2,)
    assert outcome.dropped_pages == (1,)


def test_a_document_with_no_trusted_page_has_unknown_language() -> None:
    outcome = LanguagePolicy().apply([PageLanguage(1, "unknown", 0.0)], page_count=1)

    assert outcome.language == "unknown"
    assert outcome.accepted_pages == ()
