"""Documents, pages and what an engine hands back."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Document:
    """A PDF under processing, identified by the digest of its bytes."""

    name: str
    content: bytes

    @property
    def digest(self) -> str:
        return content_digest(self.content)


def content_digest(content: bytes) -> str:
    """The Duplicate Key: the same bytes always produce the same digest."""
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class PageText:
    """The text of one page, 1-based."""

    number: int
    text: str


@dataclass(frozen=True, slots=True)
class EngineIdentity:
    """Which engine produced an extraction, and with which settings."""

    name: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.name}{'@' + self.detail if self.detail else ''}"


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """What an engine produced: page texts plus who produced them."""

    pages: tuple[PageText, ...]
    engine: EngineIdentity

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def character_count(self) -> int:
        return sum(len(page.text) for page in self.pages)


@dataclass(frozen=True, slots=True)
class TextLayerQuality:
    """What the embedded text of a PDF looks like when measured."""

    page_count: int
    pages_with_text: int
    character_count: int
    garbled_ratio: float
    image_dominated_pages: int

    @property
    def characters_per_page(self) -> float:
        return self.character_count / self.page_count if self.page_count else 0.0

    @property
    def text_coverage(self) -> float:
        return self.pages_with_text / self.page_count if self.page_count else 0.0

    @property
    def image_dominated_share(self) -> float:
        return self.image_dominated_pages / self.page_count if self.page_count else 0.0
