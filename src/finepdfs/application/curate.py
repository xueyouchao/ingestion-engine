"""Curating one document: route it, extract it, clean it, judge it.

Two steps, because they age differently. Extraction is expensive and depends only on the document and
the engine, so it is worth remembering; cleaning, language and publish rules are cheap and change
often, so they run every time. See ADR-0003.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from finepdfs.domain import text as text_rules
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
from finepdfs.domain.language import LanguagePolicy
from finepdfs.domain.ocr import OcrRoutingPolicy, RoutingDecision
from finepdfs.domain.ports import LanguageIdentifier, OcrEngine, TextLayerProbe
from finepdfs.domain.publishing import CurationOutcome, DocumentRecord, PublishPolicy

logger = logging.getLogger(__name__)

NATIVE_TEXT_ENGINE = "pymupdf"


@dataclass(frozen=True, slots=True)
class CurationPolicy:
    """The choices a run makes: which engine rescues scanned pages, and the thresholds it applies."""

    engine_name: str
    routing: OcrRoutingPolicy = field(default_factory=OcrRoutingPolicy)
    language: LanguagePolicy = field(default_factory=LanguagePolicy)
    publish: PublishPolicy = field(default_factory=PublishPolicy)


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    """What the text layer looked like, and which engine that earns."""

    quality: TextLayerQuality
    decision: RoutingDecision
    engine_name: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """An extraction and the reasoning that chose it."""

    document: ExtractedDocument
    plan: RoutingPlan

    @property
    def pages(self) -> tuple[PageText, ...]:
        return self.document.pages


def resolve_engine(engines: Mapping[str, OcrEngine], name: str) -> OcrEngine:
    """The engine a run asked for, or a message that lists what exists."""
    engine = engines.get(name)
    if engine is None:
        raise KeyError(f"unknown engine {name!r}; known engines are {', '.join(sorted(engines))}")
    return engine


class ExtractionStep:
    """Measures a PDF's text layer, decides, and runs the engine that decision earned."""

    def __init__(
        self,
        probe: TextLayerProbe,
        engines: Mapping[str, OcrEngine],
        policy: CurationPolicy,
    ) -> None:
        self._probe = probe
        self._engines = engines
        self._policy = policy

    @property
    def probe(self) -> TextLayerProbe:
        return self._probe

    @property
    def engines(self) -> Mapping[str, OcrEngine]:
        return self._engines

    @property
    def policy(self) -> CurationPolicy:
        return self._policy

    def route(self, content: bytes) -> RoutingPlan:
        quality = self._probe.measure(content)
        decision = self._policy.routing.decide(quality)
        engine_name = self._policy.engine_name if decision.needs_ocr else NATIVE_TEXT_ENGINE
        return RoutingPlan(quality=quality, decision=decision, engine_name=engine_name)

    def extract_with(self, document: Document, engine_name: str) -> ExtractedDocument:
        return resolve_engine(self._engines, engine_name).extract(document)

    def extract(self, document: Document) -> ExtractionResult:
        plan = self.route(document.content)
        return ExtractionResult(document=self.extract_with(document, plan.engine_name), plan=plan)


class CurationStep:
    """Cleans, names the language, judges and writes the record. Cheap enough to run every time."""

    def __init__(self, languages: LanguageIdentifier, policy: CurationPolicy) -> None:
        self._languages = languages
        self._policy = policy

    @property
    def languages(self) -> LanguageIdentifier:
        return self._languages

    @property
    def policy(self) -> CurationPolicy:
        return self._policy

    def clean(self, pages: tuple[PageText, ...], ocr_branch: bool) -> tuple[PageText, ...]:
        rules = text_rules.CleaningRules.for_ocr_text() if ocr_branch else text_rules.CleaningRules.for_native_text()
        return rules.clean(pages)

    def curate(self, extraction: ExtractionResult, url: str, digest: str) -> CurationOutcome:
        ocr_branch = extraction.plan.decision.needs_ocr
        pages = self.clean(extraction.pages, ocr_branch)
        tags = [self._languages.identify(page.text, page.number) for page in pages]
        language = self._policy.language.apply(tags, extraction.document.page_count)
        # The record, the publish test, the Markdown and the near-duplicate fingerprint all see the
        # accepted Page Texts only, still carrying their original 1-based numbers.
        accepted_numbers = set(language.accepted_pages)
        pages = tuple(page for page in pages if page.number in accepted_numbers)

        record = self._record(extraction, pages, language, url, digest)
        publishable, reason = self._policy.publish.judge(pages, language)
        if not publishable:
            return CurationOutcome(status="dropped", url=url, record=record, reason=reason)
        markdown = "\n\n".join(page.text for page in pages)
        return CurationOutcome(status="published", url=url, record=record, markdown=markdown, reason=reason)

    def _record(self, extraction, pages, language, url: str, digest: str) -> DocumentRecord:
        engine: EngineIdentity = extraction.document.engine
        return DocumentRecord(
            url=url,
            digest=digest,
            engine=str(engine),
            routing_decision="ocr" if extraction.plan.decision.needs_ocr else "text-layer",
            routing_reason=extraction.plan.decision.reason,
            language=language.language,
            page_count=extraction.document.page_count,
            accepted_pages=len(language.accepted_pages),
            dropped_pages=len(language.dropped_pages),
            character_count=sum(len(page.text) for page in pages),
        )


class CurateDocument:
    """The whole use case for one document, for callers that are not a CocoIndex flow."""

    def __init__(
        self,
        probe: TextLayerProbe,
        engines: Mapping[str, OcrEngine],
        languages: LanguageIdentifier,
        policy: CurationPolicy,
    ) -> None:
        self.extraction = ExtractionStep(probe, engines, policy)
        self.curation = CurationStep(languages, policy)

    def run(self, document: Document) -> CurationOutcome:
        try:
            extraction = self.extraction.extract(document)
            return self.curation.curate(extraction, url=document.name, digest=document.digest)
        except Exception as error:  # a single bad document must not end the run
            logger.warning("could not curate %s: %s", document.name, error)
            return CurationOutcome(status="failed", url=document.name, reason=f"{type(error).__name__}: {error}")
