"""StableIdentity: the engine keeps its name and its pages, the transport keeps its address to itself.

The decorator is an adapter in `infrastructure/engines/`, so its test lives here: the README's
"Dependencies point inward" holds for the tests too, and an application-layer test that imported this
adapter would be the one place that did not. The use case is imported from `application/`, which points
inward, because the thing at stake is what the published record is built from.
"""

from __future__ import annotations

from finepdfs.application.curate import NATIVE_TEXT_ENGINE, CurateDocument, CurationPolicy
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
from finepdfs.domain.language import PageLanguage
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.domain.publishing import PublishPolicy
from finepdfs.infrastructure.engines.stable_identity import StableIdentity


class _Probe:
    """Every document is a scan, so routing picks the configured engine."""

    def measure(self, content: bytes) -> TextLayerQuality:
        return TextLayerQuality(
            page_count=2, pages_with_text=0, character_count=0, garbled_ratio=0.0, image_dominated_pages=2
        )


class _AddressEngine:
    """An engine that reports the address it was reached at, as a rented worker's adapter would."""

    def __init__(self, name: str, pages: list[PageText], detail: str) -> None:
        self._identity = EngineIdentity(name=name, detail=detail)
        self._pages = pages

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document) -> ExtractedDocument:
        return ExtractedDocument(pages=tuple(self._pages), engine=self._identity)


class _Language:
    def identify(self, text: str, page_number: int) -> PageLanguage:
        return PageLanguage(page_number=page_number, language="eng", confidence=0.9)


def test_a_published_record_does_not_move_when_the_host_does() -> None:
    """ADR-0007: the record's engine is the stable identity, so a rented worker's new address is invisible.

    StableIdentity stabilised the memo key but handed back the inner engine's document, so curate.py
    wrote the transport's address-bearing engine into every published record and the records moved with
    the host each run. This drives the use case with the real decorator, which is the pair the record
    path is made of.
    """
    pages = [PageText(number=index, text=f"ocr text on page {index}\n" + "word " * 60) for index in (1, 2)]
    native = _AddressEngine(NATIVE_TEXT_ENGINE, pages, detail="native")

    def record_engine(host: str) -> str:
        engine = StableIdentity(_AddressEngine("paddleocr-vl", pages, detail=f"model@{host}"), "modal:ocr:1.6")
        policy = CurationPolicy(engine_name="paddleocr-vl", routing=OcrRoutingPolicy(), publish=PublishPolicy())
        use_case = CurateDocument(_Probe(), {NATIVE_TEXT_ENGINE: native, "paddleocr-vl": engine}, _Language(), policy)
        document = Document(name="https://a.example/scan.pdf", content=b"%PDF-1.7 bytes")
        return use_case.run(document).record.engine

    assert record_engine("https://a.example/v1") == record_engine("https://b.example/v1")
    assert record_engine("https://a.example/v1") == "paddleocr-vl@modal:ocr:1.6"
