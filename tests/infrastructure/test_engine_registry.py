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


def test_an_unknown_engine_is_rejected() -> None:
    with pytest.raises(UnknownEngine):
        build_engine(EngineConfig(name="magic-ocr"))
