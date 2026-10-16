"""The PDF adapters, checked against a real Common Crawl PDF and one built on the spot."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from finepdfs.domain.document import Document
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.infrastructure.engines.pdf_document import PdfDocument, PdfReadError, pymupdf_version
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextEngine, PymupdfTextLayerProbe

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="module")
def real_pdf_bytes() -> bytes:
    return (FIXTURES / "warc_payload.bin").read_bytes()


def _pdf_with_an_image_and_no_text() -> bytes:
    """A scanned-looking page: one picture, no text layer."""
    source = pymupdf.open()
    drawing = source.new_page(width=100, height=100)
    drawing.draw_circle(pymupdf.Point(50, 50), 40, color=(0, 0, 0), fill=(0.2, 0.4, 0.8))
    png = drawing.get_pixmap(dpi=72).tobytes("png")
    source.close()

    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(100, 100, 500, 700), stream=png)
    content = document.tobytes()
    document.close()
    return content


def test_the_real_common_crawl_pdf_has_a_healthy_text_layer(real_pdf_bytes) -> None:
    quality = PymupdfTextLayerProbe().measure(real_pdf_bytes)

    assert quality.page_count >= 1
    assert quality.characters_per_page > 200
    assert quality.text_coverage > 0.5
    assert quality.garbled_ratio < 0.05
    assert OcrRoutingPolicy().decide(quality).needs_ocr is False


def test_the_native_engine_returns_one_page_of_text_per_pdf_page(real_pdf_bytes) -> None:
    engine = PymupdfTextEngine()

    extracted = engine.extract(Document(name="real.pdf", content=real_pdf_bytes))

    with PdfDocument(real_pdf_bytes) as pdf:
        assert extracted.page_count == pdf.page_count
    assert [page.number for page in extracted.pages] == list(range(1, extracted.page_count + 1))
    assert any(page.text.strip() for page in extracted.pages)
    assert extracted.engine.name == "pymupdf"


def test_the_native_engine_identity_carries_its_version_so_upgrades_invalidate_memos() -> None:
    assert PymupdfTextEngine().identity.detail == pymupdf_version()


def test_a_page_that_is_only_an_image_is_measured_as_image_dominated() -> None:
    quality = PymupdfTextLayerProbe().measure(_pdf_with_an_image_and_no_text())

    assert quality.page_count == 1
    assert quality.pages_with_text == 0
    assert quality.image_dominated_pages == 1
    assert OcrRoutingPolicy().decide(quality).needs_ocr is True


def test_rendering_a_page_gives_a_png_a_vision_model_can_read(real_pdf_bytes) -> None:
    with PdfDocument(real_pdf_bytes) as pdf:
        image = pdf.render_page_png(1, dpi=100)

    assert image.startswith(b"\x89PNG\r\n")
    assert len(image) > 1000


def test_bytes_that_are_not_a_pdf_are_rejected_with_a_clear_error() -> None:
    with pytest.raises(PdfReadError):
        with PdfDocument(b"this is not a pdf"):
            pass
