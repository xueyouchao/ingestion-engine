"""Routing a document to the text layer or to an OCR engine."""

from __future__ import annotations

import pytest

from finepdfs.domain.document import TextLayerQuality
from finepdfs.domain.ocr import OcrRoutingPolicy


def _quality(**overrides) -> TextLayerQuality:
    defaults = dict(page_count=10, pages_with_text=10, character_count=20_000, garbled_ratio=0.001, image_dominated_pages=0)
    return TextLayerQuality(**{**defaults, **overrides})


def test_a_healthy_text_layer_is_not_sent_to_ocr() -> None:
    decision = OcrRoutingPolicy().decide(_quality())

    assert decision.needs_ocr is False
    assert "healthy" in decision.reason
    assert decision.features["characters_per_page"] == 2000.0


@pytest.mark.parametrize(
    "quality, expected_words",
    [
        (_quality(pages_with_text=2), "pages carry text"),
        (_quality(character_count=500), "characters per page"),
        (_quality(garbled_ratio=0.4), "garbled"),
        (_quality(image_dominated_pages=9), "images with little text"),
        (_quality(page_count=0, pages_with_text=0, character_count=0), "no pages"),
    ],
)
def test_each_way_a_text_layer_can_be_unusable_triggers_ocr(quality, expected_words) -> None:
    decision = OcrRoutingPolicy().decide(quality)

    assert decision.needs_ocr is True
    assert expected_words in decision.reason


def test_thresholds_are_policy_not_constants() -> None:
    quality = _quality(character_count=1500)

    assert OcrRoutingPolicy().decide(quality).needs_ocr is True
    assert OcrRoutingPolicy(min_characters_per_page=100).decide(quality).needs_ocr is False
