"""Which engine a run gets for a given configuration: the flag a user types must reach a real engine."""

from __future__ import annotations

import pytest

from finepdfs.infrastructure.engines.paddleocr_vl import PaddleOcrVlCliEngine, PaddleOcrVlEngine
from finepdfs.infrastructure.engines.registry import EngineConfig, UnknownEngine, build_engine


def test_paddleocr_vl_is_reached_over_http_by_default() -> None:
    engine = build_engine(
        EngineConfig(name="paddleocr-vl", base_url="http://127.0.0.1:8000/v1", model="PaddleOCR-VL-1.6")
    )

    assert isinstance(engine, PaddleOcrVlEngine)
    assert "PaddleOCR-VL-1.6" in str(engine.identity)


def test_paddleocr_vl_with_a_command_uses_the_local_tool() -> None:
    """The documented CLI route has to be reachable, not just implemented."""
    engine = build_engine(EngineConfig(name="paddleocr-vl", command="paddleocr"))

    assert isinstance(engine, PaddleOcrVlCliEngine)
    assert "paddleocr" in str(engine.identity)


def test_a_different_model_is_a_different_engine_identity() -> None:
    """The memo key is the identity, so a model switch must be a different key."""
    first = build_engine(EngineConfig(name="paddleocr-vl", base_url="http://host:8000/v1", model="PaddleOCR-VL-1.6"))
    second = build_engine(EngineConfig(name="paddleocr-vl", base_url="http://host:8000/v1", model="PaddleOCR-VL-1.5"))

    assert str(first.identity) != str(second.identity)


def test_a_page_dpi_change_moves_the_engine_identity() -> None:
    """ADR-0005 and the spec list page DPI among the identity's inputs, because the text depends on it.

    A vision model reads the page at this DPI, so the same bytes at 300 DPI can produce different text
    than at 200. Both ways the detail is decided must carry it: the adapter's own default, and the
    deployment name a lease pins, which replaces that default and would otherwise drop the DPI.
    """
    served = {"name": "paddleocr-vl", "base_url": "http://host:8000/v1", "model": "m"}
    assert str(build_engine(EngineConfig(**served)).identity) != str(
        build_engine(EngineConfig(**served, page_dpi=300)).identity
    )

    pinned = dict(served, detail="modal:ocr:1.6")
    assert str(build_engine(EngineConfig(**pinned)).identity) != str(
        build_engine(EngineConfig(**pinned, page_dpi=300)).identity
    )


def test_an_unknown_engine_is_rejected() -> None:
    with pytest.raises(UnknownEngine):
        build_engine(EngineConfig(name="magic-ocr"))
