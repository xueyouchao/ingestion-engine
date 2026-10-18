"""Which engine a run gets for a given configuration: the flag a user types must reach a real engine."""

from __future__ import annotations

import sys

import pytest

from finepdfs.infrastructure.engines.paddleocr_vl import PaddleOcrVlCliEngine, PaddleOcrVlEngine
from finepdfs.infrastructure.engines.registry import (
    DPI_CARRYING_ENGINES,
    KNOWN_ENGINES,
    EngineConfig,
    UnknownEngine,
    build_engine,
)


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


def _reachable_config(name: str, *, page_dpi: int, detail: str = "") -> EngineConfig:
    """A configuration that builds each engine without running anything: a URL for the HTTP ones.

    Docling's identity is probed from its command, and a probe that cannot answer is an error rather
    than an identity without a version (ADR-0005), so the adapter is pointed at an executable that is
    certainly installed here. What it prints is irrelevant to this test: the question is whether a page
    DPI moves the identity, and this engine does not render at one.
    """
    if name == "paddleocr-vl":
        return EngineConfig(name=name, base_url="http://host:8000/v1", model="m", page_dpi=page_dpi, detail=detail)
    if name == "mineru":
        return EngineConfig(name=name, base_url="http://host:8001", page_dpi=page_dpi, detail=detail)
    if name == "docling":
        return EngineConfig(name=name, command=sys.executable, page_dpi=page_dpi, detail=detail)
    return EngineConfig(name=name, page_dpi=page_dpi, detail=detail)


def test_only_the_engines_that_render_at_a_dpi_carry_one() -> None:
    """``DPI_CARRYING_ENGINES`` lists every engine whose identity carries a page DPI, and nothing else does.

    ADR-0005 makes page DPI an identity input, and ``_deployment_detail`` can append ``@dpi{dpi}`` to a
    pinned detail only for an engine it knows renders at a DPI. An engine that starts carrying a DPI in
    its own detail without joining that tuple would have the DPI silently dropped from a pinned
    ``--engine-detail``, and a change to ``FINEPDFS_ENGINE_DPI`` on it would be served the other DPI's
    text from the memo. So every engine is checked on two observable signals -- whether its detail
    carries a ``@dpi`` marker, and whether a ``page_dpi`` change moves its identity, pinned or not --
    and both have to match the registry's list.
    """
    for name in KNOWN_ENGINES:
        carries = name in DPI_CARRYING_ENGINES
        low = build_engine(_reachable_config(name, page_dpi=200))
        high = build_engine(_reachable_config(name, page_dpi=300))
        pinned_low = build_engine(_reachable_config(name, page_dpi=200, detail="pinned:v1"))
        pinned_high = build_engine(_reachable_config(name, page_dpi=300, detail="pinned:v1"))
        observed = {
            "the adapter's detail carries @dpi": "@dpi" in low.identity.detail,
            "a page_dpi change moves the adapter's identity": low.identity != high.identity,
            "a page_dpi change moves a pinned identity": pinned_low.identity != pinned_high.identity,
        }
        assert all(value == carries for value in observed.values()), (
            f"{name!r}: DPI_CARRYING_ENGINES says it carries a page DPI is {carries}, but "
            f"{ {signal: value for signal, value in observed.items() if value != carries} }; an engine "
            f"that renders at a DPI has to join that tuple and be handled by _deployment_detail"
        )
