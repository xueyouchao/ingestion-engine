"""Turning per-page language tags into a document language. Pure policy."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence

UNKNOWN_LANGUAGE = "unknown"


@dataclass(frozen=True, slots=True)
class PageLanguage:
    """The language identified for one page, with a confidence."""

    page_number: int
    language: str
    confidence: float


@dataclass(frozen=True, slots=True)
class LanguageOutcome:
    """The document's language, and which pages were trusted enough to say so."""

    language: str
    accepted_pages: tuple[int, ...]
    dropped_pages: tuple[int, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class LanguagePolicy:
    """Which page languages we trust, and how a document language is chosen."""

    min_confidence: float = 0.65

    def apply(self, tags: Sequence[PageLanguage], page_count: int) -> LanguageOutcome:
        trusted = [tag for tag in tags if tag.confidence >= self.min_confidence and tag.language != UNKNOWN_LANGUAGE]

        if not trusted:
            return LanguageOutcome(UNKNOWN_LANGUAGE, (), tuple(tag.page_number for tag in tags), 0.0)

        counts = Counter(tag.language for tag in trusted)
        language, _ = counts.most_common(1)[0]
        accepted = tuple(tag.page_number for tag in trusted if tag.language == language)
        dropped = tuple(tag.page_number for tag in tags if tag.page_number not in set(accepted))
        confidence = sum(tag.confidence for tag in trusted if tag.language == language) / len(accepted)
        return LanguageOutcome(language, accepted, dropped, confidence)
