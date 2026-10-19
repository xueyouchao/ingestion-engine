"""One reader for an engine's environment, so a run and the demo server reach the same engine.

The flow is configured entirely by `FINEPDFS_*` names, because `cocoindex update main.py` takes no
arguments of its own, and the demo server reads the same names so a Reading and a run are comparable.
Both call this module: the mapping lives here once, and either consumer that inlined it again could
silently drift into a different base URL, model, page DPI or routing threshold (ADR-0012).

Only the engine's name is the caller's decision. A run defaults to the native text layer, the demo's
OCR lane to the deployed engine, so the name is an argument rather than something read here.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping

from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.infrastructure.compute.endpoint import (
    ENGINE_API_KEY,
    ENGINE_BASE_URL,
    ENGINE_DETAIL,
    ENGINE_MODEL,
)
from finepdfs.infrastructure.engines.concurrency import ENGINE_CONCURRENCY, engine_concurrency
from finepdfs.infrastructure.engines.registry import EngineConfig

ENGINE_COMMAND = "FINEPDFS_ENGINE_COMMAND"
ENGINE_DPI = "FINEPDFS_ENGINE_DPI"
ENGINE_TIMEOUT = "FINEPDFS_ENGINE_TIMEOUT"
ENGINE_WORK_DIR = "FINEPDFS_ENGINE_WORK_DIR"
MIN_CHARACTERS_PER_PAGE = "FINEPDFS_MIN_CHARACTERS_PER_PAGE"
MIN_TEXT_COVERAGE = "FINEPDFS_MIN_TEXT_COVERAGE"
MAX_GARBLED_RATIO = "FINEPDFS_MAX_GARBLED_RATIO"

DEFAULT_PAGE_DPI = 200
DEFAULT_TIMEOUT_SECONDS = 300.0


def engine_config(values: Mapping[str, str], name: str) -> EngineConfig:
    """The Engine Config for one engine name, read from the values a run was given."""
    work_dir = _configured(values, ENGINE_WORK_DIR)
    return EngineConfig(
        name=name,
        command=_configured(values, ENGINE_COMMAND),
        # The seam's four names come from where they are written (compute.endpoint), not from a literal
        # spelled again here: one owner, so a rename does not have to be found in this file too.
        base_url=_configured(values, ENGINE_BASE_URL),
        model=_configured(values, ENGINE_MODEL),
        api_key=_configured(values, ENGINE_API_KEY),
        page_dpi=int(_configured(values, ENGINE_DPI) or DEFAULT_PAGE_DPI),
        concurrency=engine_concurrency(_configured(values, ENGINE_CONCURRENCY)),
        work_dir=pathlib.Path(work_dir) if work_dir else None,
        timeout_seconds=float(_configured(values, ENGINE_TIMEOUT) or DEFAULT_TIMEOUT_SECONDS),
        detail=_configured(values, ENGINE_DETAIL),
    )


def ocr_routing_policy(values: Mapping[str, str]) -> OcrRoutingPolicy:
    """The OCR Routing thresholds one run applies, from the one set of names a demo reads too."""
    return OcrRoutingPolicy(
        min_characters_per_page=float(_configured(values, MIN_CHARACTERS_PER_PAGE) or 200.0),
        min_text_coverage=float(_configured(values, MIN_TEXT_COVERAGE) or 0.5),
        max_garbled_ratio=float(_configured(values, MAX_GARBLED_RATIO) or 0.05),
    )


def _configured(values: Mapping[str, str], variable: str) -> str:
    """The value that was actually set: an absent or blank one means the variable's default.

    A variable exported with no value is how an operator asks for the default, so it must read as
    absent rather than as the empty string a parser would reject.
    """
    text = values.get(variable, "")
    return text if text.strip() else ""
