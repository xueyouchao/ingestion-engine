"""The one page projection a Published Document's Markdown and the demo's document pane share.

ADR-0014: the corpus and the demo must not restate the ordering or the join rule, so the rule lives in
one function and both callers are proven to call it. A second join in the demo could show a visitor a
document the corpus would never publish.
"""

from __future__ import annotations

import finepdfs.application.curate as curate
from finepdfs.application.curate import CurationPolicy, CurationStep, ExtractionResult, RoutingPlan
from finepdfs.domain.document import EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
from finepdfs.domain.language import PageLanguage
from finepdfs.domain.ocr import RoutingDecision
from finepdfs.domain.publishing import document_markdown

HEALTHY = TextLayerQuality(
    page_count=3, pages_with_text=3, character_count=4000, garbled_ratio=0.0, image_dominated_pages=0
)


class FakeLanguage:
    """Every page is confident English, so the projection sees all three pages."""

    def identify(self, text: str, page_number: int) -> PageLanguage:
        return PageLanguage(page_number=page_number, language="eng", confidence=0.9)


def _extraction(pages: tuple[PageText, ...]) -> ExtractionResult:
    decision = RoutingDecision(False, "the text layer is healthy", {"page_count": float(len(pages))})
    return ExtractionResult(
        document=ExtractedDocument(pages=pages, engine=EngineIdentity("pymupdf", "fake")),
        plan=RoutingPlan(quality=HEALTHY, decision=decision, engine_name="pymupdf"),
    )


def test_pages_project_in_number_order_with_a_blank_line_between_them() -> None:
    """The rule the Published Document's Markdown already had: page order, then two newlines."""
    pages = (PageText(3, "third page"), PageText(1, "first page"), PageText(2, "second page"))

    assert document_markdown(pages) == "first page\n\nsecond page\n\nthird page"


def test_curating_a_document_projects_its_pages_with_the_shared_function(monkeypatch) -> None:
    """A second join in curation would be a projection the demo could drift from; there is none."""
    pages = (
        PageText(3, "third page. " + "word " * 40),
        PageText(1, "first page. " + "word " * 40),
        PageText(2, "second page. " + "word " * 40),
    )
    calls: list[tuple[tuple[int, str], ...]] = []

    def projection(projected) -> str:
        calls.append(tuple((page.number, page.text) for page in projected))
        return "the shared projection's answer"

    monkeypatch.setattr(curate, "document_markdown", projection)
    step = CurationStep(FakeLanguage(), CurationPolicy(engine_name="paddleocr-vl"))

    outcome = step.curate(_extraction(pages), url="https://example.org/one.pdf", digest="DIGEST")

    assert outcome.published is True
    assert outcome.markdown == "the shared projection's answer"
    # Cleaning runs before the projection, so the texts differ in whitespace; the pages and their
    # original numbers are what prove curation handed its own accepted pages to the one projection.
    assert [numbers for numbers, _ in calls[0]] == [3, 1, 2]
    assert len(calls) == 1