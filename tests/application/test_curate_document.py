"""The pipeline use case, driven through ports with hand-written fakes.

This is the main seam: no network, no models, no CocoIndex, no filesystem.
"""

from __future__ import annotations

from finepdfs.application.curate import NATIVE_TEXT_ENGINE, CurateDocument, CurationPolicy
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
from finepdfs.domain.language import PageLanguage
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.domain.publishing import PublishPolicy


class FakeProbe:
    def __init__(self, quality: TextLayerQuality) -> None:
        self._quality = quality
        self.measured: list[int] = []

    def measure(self, content: bytes) -> TextLayerQuality:
        self.measured.append(len(content))
        return self._quality


class FakeEngine:
    def __init__(self, name: str, pages, detail: str = "") -> None:
        self._identity = EngineIdentity(name=name, detail=detail)
        self._pages = pages
        self.calls = 0
        self.failure: Exception | None = None

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document) -> ExtractedDocument:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return ExtractedDocument(pages=tuple(self._pages), engine=self._identity)


class FakeLanguage:
    def __init__(self, language: str = "eng", confidence: float = 0.9) -> None:
        self._language, self._confidence = language, confidence
        self.seen_pages: list[int] = []

    def identify(self, text: str, page_number: int) -> PageLanguage:
        self.seen_pages.append(page_number)
        return PageLanguage(page_number=page_number, language=self._language, confidence=self._confidence)


HEALTHY = TextLayerQuality(page_count=2, pages_with_text=2, character_count=4000, garbled_ratio=0.0, image_dominated_pages=0)
SCANNED = TextLayerQuality(page_count=2, pages_with_text=0, character_count=0, garbled_ratio=0.0, image_dominated_pages=2)


def _documents_pages(prefix: str = "body") -> list[PageText]:
    return [PageText(number=index, text=f"{prefix} text on page {index}\n" + "word " * 60) for index in (1, 2)]


def _use_case(probe_quality, engines, languages=None, engine="paddleocr-vl", **policy_overrides) -> tuple[CurateDocument, dict]:
    engines.setdefault(NATIVE_TEXT_ENGINE, FakeEngine(NATIVE_TEXT_ENGINE, _documents_pages("native")))
    policy = CurationPolicy(
        engine_name=engine,
        routing=policy_overrides.pop("routing", OcrRoutingPolicy()),
        publish=policy_overrides.pop("publish", PublishPolicy()),
    )
    languages = languages or FakeLanguage()
    return CurateDocument(FakeProbe(probe_quality), engines, languages, policy), engines


def test_a_healthy_document_is_published_by_the_text_layer_engine() -> None:
    ocr = FakeEngine("paddleocr-vl", _documents_pages("ocr"))
    use_case, engines = _use_case(HEALTHY, {"paddleocr-vl": ocr, NATIVE_TEXT_ENGINE: FakeEngine(NATIVE_TEXT_ENGINE, _documents_pages("native"))})

    outcome = use_case.run(Document(name="https://a.example/one.pdf", content=b"%PDF-1.7 bytes"))

    assert outcome.published is True
    assert engines[NATIVE_TEXT_ENGINE].calls == 1
    assert ocr.calls == 0
    assert outcome.record.engine == NATIVE_TEXT_ENGINE
    assert outcome.record.routing_decision == "text-layer"
    assert "native text on page 1" in outcome.markdown


def test_a_scanned_document_is_published_by_the_configured_engine() -> None:
    ocr = FakeEngine("paddleocr-vl", _documents_pages("ocr"), detail="http://localhost:8080/v1")
    use_case, engines = _use_case(SCANNED, {"paddleocr-vl": ocr, NATIVE_TEXT_ENGINE: FakeEngine(NATIVE_TEXT_ENGINE, _documents_pages("native"))})

    outcome = use_case.run(Document(name="https://a.example/scan.pdf", content=b"%PDF-1.7 bytes"))

    assert outcome.published is True
    assert ocr.calls == 1
    assert engines[NATIVE_TEXT_ENGINE].calls == 0
    assert outcome.record.engine == "paddleocr-vl@http://localhost:8080/v1"
    assert outcome.record.routing_decision == "ocr"
    assert "ocr text on page 1" in outcome.markdown


def test_the_configured_engine_is_used_when_routing_asks_for_ocr() -> None:
    ocr = FakeEngine("mineru", _documents_pages("mineru"))
    use_case, _ = _use_case(
        SCANNED,
        {"mineru": ocr, NATIVE_TEXT_ENGINE: FakeEngine(NATIVE_TEXT_ENGINE, _documents_pages())},
        engine="mineru",
    )

    outcome = use_case.run(Document(name="https://a.example/scan.pdf", content=b"%PDF"))

    assert ocr.calls == 1
    assert outcome.record.engine == "mineru"


def test_the_record_describes_the_document_and_the_routing() -> None:
    use_case, _ = _use_case(HEALTHY, {NATIVE_TEXT_ENGINE: FakeEngine(NATIVE_TEXT_ENGINE, _documents_pages())})

    outcome = use_case.run(Document(name="https://a.example/one.pdf", content=b"%PDF-1.7 bytes"))

    record = outcome.record
    assert record.url == "https://a.example/one.pdf"
    assert record.digest == Document(name="x", content=b"%PDF-1.7 bytes").digest
    assert record.page_count == 2
    assert record.language == "eng"
    assert record.character_count > 200
    assert "healthy" in record.routing_reason


def test_every_page_is_offered_to_the_language_identifier() -> None:
    languages = FakeLanguage()
    use_case, _ = _use_case(HEALTHY, {NATIVE_TEXT_ENGINE: FakeEngine(NATIVE_TEXT_ENGINE, _documents_pages())}, languages=languages)

    use_case.run(Document(name="https://a.example/one.pdf", content=b"%PDF"))

    assert languages.seen_pages == [1, 2]


def test_a_document_the_policy_drops_is_not_a_failure() -> None:
    use_case, _ = _use_case(
        HEALTHY,
        {NATIVE_TEXT_ENGINE: FakeEngine(NATIVE_TEXT_ENGINE, _documents_pages())},
        languages=FakeLanguage(language="unknown", confidence=0.0),
    )

    outcome = use_case.run(Document(name="https://a.example/one.pdf", content=b"%PDF"))

    assert outcome.status == "dropped"
    assert outcome.markdown == ""
    assert "language" in outcome.reason


def test_an_engine_failure_is_reported_as_a_failed_document_not_an_exception() -> None:
    failing = FakeEngine(NATIVE_TEXT_ENGINE, _documents_pages())
    failing.failure = RuntimeError("the text layer exploded")
    use_case, _ = _use_case(HEALTHY, {NATIVE_TEXT_ENGINE: failing})

    outcome = use_case.run(Document(name="https://a.example/one.pdf", content=b"%PDF"))

    assert outcome.status == "failed"
    assert "the text layer exploded" in outcome.reason


def test_an_unknown_engine_is_reported_as_a_failed_document() -> None:
    use_case, _ = _use_case(SCANNED, {NATIVE_TEXT_ENGINE: FakeEngine(NATIVE_TEXT_ENGINE, _documents_pages())})

    outcome = use_case.run(Document(name="https://a.example/one.pdf", content=b"%PDF"))

    assert outcome.status == "failed"
    assert "unknown engine 'paddleocr-vl'" in outcome.reason


def test_a_probe_failure_is_reported_as_a_failed_document() -> None:
    class ExplodingProbe(FakeProbe):
        def measure(self, content: bytes):
            raise ValueError("this file is not a PDF")

    use_case = CurateDocument(
        ExplodingProbe(HEALTHY),
        {NATIVE_TEXT_ENGINE: FakeEngine(NATIVE_TEXT_ENGINE, _documents_pages())},
        FakeLanguage(),
        CurationPolicy(engine_name="paddleocr-vl"),
    )

    outcome = use_case.run(Document(name="https://a.example/broken.pdf", content=b"not a pdf"))

    assert outcome.status == "failed"
    assert "not a PDF" in outcome.reason
