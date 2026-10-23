"""What a published document looks like, and which documents get published. Pure."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from finepdfs.domain.document import PageText
from finepdfs.domain.language import LanguageOutcome

OutcomeStatus = Literal["published", "dropped", "failed"]

# The blank line a Published Document's Markdown puts between two Page Texts. Named because the demo's
# document pane offers the same bytes for download, so the one projection below is the only place the
# join rule can change (ADR-0014).
PAGE_SEPARATOR = "\n\n"


def pages_in_order(pages: Iterable[PageText]) -> tuple[PageText, ...]:
    """Page Texts in their original 1-based *numeric* order, whichever order they arrived in.

    The one ordering rule, beside the one join rule: a page that arrives out of order still lands where
    its number says, numerically -- page 2 before page 10 -- and the corpus, the demo's document pane and
    the job answer all read this function rather than restating a sort (ADR-0014).
    """
    return tuple(sorted(pages, key=lambda page: page.number))


def document_markdown(pages: Sequence[PageText]) -> str:
    """A Published Document's Markdown: its Page Texts in page-number order, blank-line joined.

    One function because the corpus and the demo's document pane must not restate the ordering or the
    join rule: a page that arrives out of order still lands where its 1-based number says, and a
    projection read from the demo is the same bytes the corpus would write (ADR-0014).
    """
    return PAGE_SEPARATOR.join(page.text for page in pages_in_order(pages))


@dataclass(frozen=True, slots=True)
class PublishPolicy:
    """The minimum a document must be to enter the corpus."""

    min_pages: int = 1
    min_characters: int = 200
    required_languages: tuple[str, ...] = ()

    def judge(self, pages: Sequence[PageText], language: LanguageOutcome) -> tuple[bool, str]:
        text = "\n".join(page.text for page in pages)
        if len(pages) < self.min_pages:
            return False, f"{len(pages)} pages is fewer than the {self.min_pages} page minimum"
        if len(text) < self.min_characters:
            return False, f"{len(text)} characters is fewer than the {self.min_characters} character minimum"
        if language.language == "unknown":
            return False, "no page carried a language confident enough to publish"
        if self.required_languages and language.language not in self.required_languages:
            allowed = ", ".join(self.required_languages)
            return False, f"language {language.language} is not one of the required languages ({allowed})"
        return True, "published"


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """One line of published metadata: what this document is and how it was produced."""

    url: str
    digest: str
    engine: str
    routing_decision: str
    routing_reason: str
    language: str
    page_count: int
    accepted_pages: int
    dropped_pages: int
    character_count: int

    def as_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PageRecord:
    """One accepted Page Text as a reader cites it: whose page it is and where the text came from."""

    doc_id: str
    source_url: str
    digest: str
    page_number: int
    text: str
    engine: str

    def as_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CurationOutcome:
    """What curating one document produced.

    ``pages`` are the accepted Page Texts the Markdown projects, still carrying their original 1-based
    numbers, so a published Document can export them. A document that is not published exports none.
    """

    status: OutcomeStatus
    url: str
    record: DocumentRecord | None = None
    pages: tuple[PageText, ...] = ()
    markdown: str = ""
    reason: str = ""

    @property
    def published(self) -> bool:
        return self.status == "published"
