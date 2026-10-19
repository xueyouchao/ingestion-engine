"""An engine's environment has one reader, so a run and the demo reach the same engine.

The mapping used to live inside the flow's runtime builder; these tests pin the standalone readers
against the values they read and pin the flow's runtime to the same readers.
"""

from __future__ import annotations

from pathlib import Path

from finepdfs.domain.ocr import OcrRoutingPolicy
from finepdfs.infrastructure.cocoindex.runtime import runtime_from_environment
from finepdfs.infrastructure.engines.environment import engine_config, ocr_routing_policy
from finepdfs.infrastructure.engines.registry import EngineConfig, build_engines

# One fixed environment: every name an engineer can set, each with a value that differs from its
# default so a reader that forgot a name cannot pass by accident.
ENVIRONMENT = {
    "FINEPDFS_ENGINE": "paddleocr-vl",
    "FINEPDFS_ENGINE_BASE_URL": "https://ocr.example/v1",
    "FINEPDFS_ENGINE_MODEL": "PaddleOCR-VL-1.6",
    "FINEPDFS_ENGINE_API_KEY": "secret-token",
    "FINEPDFS_ENGINE_DETAIL": "modal:ocr:1.6",
    "FINEPDFS_ENGINE_DPI": "300",
    "FINEPDFS_ENGINE_CONCURRENCY": "4",
    "FINEPDFS_ENGINE_TIMEOUT": "90",
    "FINEPDFS_ENGINE_WORK_DIR": "/tmp/engine-work",
    "FINEPDFS_MIN_CHARACTERS_PER_PAGE": "120",
    "FINEPDFS_MIN_TEXT_COVERAGE": "0.4",
    "FINEPDFS_MAX_GARBLED_RATIO": "0.02",
}


def test_the_engine_config_reader_reads_every_variable_it_owns() -> None:
    """Every FINEPDFS_ENGINE_* name lands in its own field of the config the registry expects."""
    assert engine_config(ENVIRONMENT, "paddleocr-vl") == EngineConfig(
        name="paddleocr-vl",
        base_url="https://ocr.example/v1",
        model="PaddleOCR-VL-1.6",
        api_key="secret-token",
        page_dpi=300,
        concurrency=4,
        timeout_seconds=90.0,
        work_dir=Path("/tmp/engine-work"),
        detail="modal:ocr:1.6",
    )


def test_the_routing_policy_reader_reads_every_threshold_it_owns() -> None:
    """The three OCR Routing thresholds come from their one set of names and defaults."""
    assert ocr_routing_policy(ENVIRONMENT) == OcrRoutingPolicy(
        min_characters_per_page=120.0,
        min_text_coverage=0.4,
        max_garbled_ratio=0.02,
    )


BLANK = dict.fromkeys(ENVIRONMENT, "")


def test_a_blank_value_means_the_default_for_every_variable() -> None:
    """A variable exported with no value is how an operator asks for the default, not a crash."""
    assert engine_config(BLANK, "paddleocr-vl") == EngineConfig(name="paddleocr-vl")
    assert ocr_routing_policy(BLANK) == OcrRoutingPolicy()


def test_the_engine_name_is_the_callers_decision_not_the_environments() -> None:
    """A run defaults to the native text layer and the demo's OCR lane to the deployed engine."""
    values = dict(BLANK, FINEPDFS_ENGINE="docling")

    assert engine_config(values, "paddleocr-vl").name == "paddleocr-vl"
    assert engine_config(values, "pymupdf").name == "pymupdf"


def test_the_runtime_agrees_with_the_standalone_readers_on_a_fixed_environment() -> None:
    """The flow's engine and routing policy are the readers' output, not a second copy of the mapping.

    Only the engine's identity and its bound are observable from the assembled runtime -- the API key
    and timeout are transport, not identity (ADR-0005) -- so those are what the two consumers are
    compared on, while the readers' exact fields are pinned by the tests above.
    """
    runtime = runtime_from_environment(ENVIRONMENT)
    standalone = build_engines([engine_config(ENVIRONMENT, "paddleocr-vl")])["paddleocr-vl"]

    assert runtime.policy.routing == ocr_routing_policy(ENVIRONMENT)
    assert runtime.engines["paddleocr-vl"].identity == standalone.identity
    assert runtime.engines["paddleocr-vl"].identity.detail == "modal:ocr:1.6@dpi300"

    # A pinned detail wraps the engine in StableIdentity, which delegates extraction and holds no bound
    # of its own, so the engine's bound is compared on the same environment without a pin.
    unpinned = dict(ENVIRONMENT, FINEPDFS_ENGINE_DETAIL="")
    runtime_unpinned = runtime_from_environment(unpinned)
    standalone_unpinned = build_engines([engine_config(unpinned, "paddleocr-vl")])["paddleocr-vl"]

    assert runtime_unpinned.engines["paddleocr-vl"].calls.limit == standalone_unpinned.calls.limit == 4


def test_the_runtime_builds_from_a_blank_environment_without_raising() -> None:
    """Blank FINEPDFS_ENGINE keeps the run on the native text layer instead of naming an unknown engine."""
    runtime = runtime_from_environment(dict(BLANK))

    assert runtime.policy.engine_name == "pymupdf"
    assert set(runtime.engines) == {"pymupdf"}

    configured = runtime_from_environment(dict(BLANK, FINEPDFS_ENGINE="paddleocr-vl"))

    assert configured.engines["paddleocr-vl"].identity.detail == "@@dpi200"
    assert configured.engines["paddleocr-vl"].calls.limit == 6
