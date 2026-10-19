"""The PDF adapters, checked against a real Common Crawl PDF and one built on the spot."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from finepdfs.domain.document import Document
from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.infrastructure.engines.pdf_document import DocumentReadError, MuPDFDocument, pymupdf_version
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

    with MuPDFDocument(real_pdf_bytes) as pdf:
        assert extracted.page_count == pdf.page_count
    assert [page.number for page in extracted.pages] == list(range(1, extracted.page_count + 1))
    assert any(page.text.strip() for page in extracted.pages)
    assert extracted.engine.name == "pymupdf"


def test_the_native_engine_identity_is_reproducible_and_carries_the_version() -> None:
    """The identity is what the memo key holds, so it must be reproducible and carry the version."""
    identity = PymupdfTextEngine().identity

    assert identity == PymupdfTextEngine().identity
    assert identity.name == "pymupdf"
    assert pymupdf_version() in identity.detail


def test_a_page_that_is_only_an_image_is_measured_as_image_dominated() -> None:
    quality = PymupdfTextLayerProbe().measure(_pdf_with_an_image_and_no_text())

    assert quality.page_count == 1
    assert quality.pages_with_text == 0
    assert quality.image_dominated_pages == 1
    assert OcrRoutingPolicy().decide(quality).needs_ocr is True


def test_rendering_a_page_gives_a_png_a_vision_model_can_read(real_pdf_bytes) -> None:
    with MuPDFDocument(real_pdf_bytes) as pdf:
        image = pdf.render_page_png(1, dpi=100)

    assert image.startswith(b"\x89PNG\r\n")
    assert len(image) > 1000


def test_bytes_that_are_not_a_pdf_are_rejected_with_a_clear_error() -> None:
    with pytest.raises(DocumentReadError), MuPDFDocument(b"this is not a pdf"):
        pass


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.parametrize(
    ("name", "pages"),
    [
        ("scan.png", 1),
        ("scan.jpg", 1),
        ("scan-3-pages.tiff", 3),
    ],
)
def test_a_scanned_image_is_a_page_with_no_text_layer(name: str, pages: int) -> None:
    """A picture of a page is a page: the reader takes one page per frame, and the page has no text.

    That is the whole reason the demo can take a scan at all -- and why the Routing Decision sends one to
    the OCR lane without any special case. A multi-frame TIFF counts one page per frame, so the page cap
    and the OCR budget count what the reader really found.
    """
    content = _fixture(name)

    with MuPDFDocument(content) as document:
        assert document.page_count == pages
        assert [page.number for page in document.page_texts()] == list(range(1, pages + 1))
        assert all(page.text == "" for page in document.page_texts())
        assert len(document.render_page_png(1, dpi=100)) > 0, "the OCR lane needs a render of the frame"

    quality = PymupdfTextLayerProbe().measure(content)

    assert quality.page_count == pages
    assert quality.character_count == 0, "a picture of a page carries no characters at all"
    assert quality.text_coverage == 0.0
    assert OcrRoutingPolicy().decide(quality).needs_ocr is True


def test_bytes_that_are_neither_a_pdf_nor_an_image_are_unreadable() -> None:
    with pytest.raises(DocumentReadError, match="not a readable PDF or image"), MuPDFDocument(b"neither"):
        pass
