"""One run's wiring: where the corpus is, where output goes, and which engine rescues scans.

Everything is read from FINEPDFS_* environment variables, because `cocoindex update main.py` takes no
arguments of its own. Nothing here is a domain decision: it only assembles the adapters.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass

from finepdfs.application.curate import CurateDocument, CurationPolicy, CurationStep, ExtractionStep
from finepdfs.domain.language import LanguagePolicy
from finepdfs.domain.ports import OcrEngine
from finepdfs.domain.publishing import PublishPolicy
from finepdfs.infrastructure.compute.endpoint import (
    ENGINE_API_KEY,
    ENGINE_BASE_URL,
    ENGINE_IDENTITY,
    ENGINE_MODEL,
)
from finepdfs.infrastructure.corpus.index import CorpusIndex
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextLayerProbe
from finepdfs.infrastructure.engines.registry import EngineConfig, build_engines
from finepdfs.infrastructure.language.registry import (
    DEFAULT_LANGUAGE_MODE,
    LanguageConfig,
    build_language_identifier,
)
from finepdfs.infrastructure.publishing.disk import PublishedCorpus

DEFAULT_CORPUS_DIR = "corpus"
DEFAULT_OUT_DIR = "out"
DEFAULT_DB_PATH = "cocoindex.db"


@dataclass(frozen=True, slots=True)
class Runtime:
    """The assembled pipeline for one run."""

    corpus_dir: pathlib.Path
    out_dir: pathlib.Path
    policy: CurationPolicy
    extraction: ExtractionStep
    curation: CurationStep
    similarity_threshold: float = 0.8

    @property
    def engines(self) -> Mapping[str, OcrEngine]:
        return self.extraction.engines

    def corpus(self) -> CorpusIndex:
        return CorpusIndex(self.corpus_dir)

    def published(self) -> PublishedCorpus:
        return PublishedCorpus(self.out_dir)

    def curator(self) -> CurateDocument:
        """The whole-document use case, for callers that are not the flow (dry runs, scripts)."""
        return CurateDocument(
            self.extraction.probe, self.extraction.engines, self.curation.languages, self.policy
        )


def runtime_from_environment(environ: Mapping[str, str] | None = None) -> Runtime:
    """Build the runtime from the environment. The engine defaults to the free text layer."""
    values = environ if environ is not None else os.environ
    corpus_dir = pathlib.Path(values.get("FINEPDFS_CORPUS", DEFAULT_CORPUS_DIR))
    out_dir = pathlib.Path(values.get("FINEPDFS_OUT", DEFAULT_OUT_DIR))
    engine_name = values.get("FINEPDFS_ENGINE", "pymupdf")
    language_config = LanguageConfig(
        mode=values.get("FINEPDFS_LANGUAGE", DEFAULT_LANGUAGE_MODE),
        model_path=values.get("FINEPDFS_LANGUAGE_MODEL", ""),
    )

    engine_config = EngineConfig(
        name=engine_name,
        command=values.get("FINEPDFS_ENGINE_COMMAND", ""),
        # The seam's four names come from where they are written (compute.endpoint), not from a literal
        # spelled again here: one owner, so a rename does not have to be found in this file too.
        base_url=values.get(ENGINE_BASE_URL, ""),
        model=values.get(ENGINE_MODEL, ""),
        api_key=values.get(ENGINE_API_KEY, ""),
        page_dpi=int(values.get("FINEPDFS_ENGINE_DPI", "200")),
        work_dir=pathlib.Path(values["FINEPDFS_ENGINE_WORK_DIR"]) if values.get("FINEPDFS_ENGINE_WORK_DIR") else None,
        timeout_seconds=float(values.get("FINEPDFS_ENGINE_TIMEOUT", "300")),
        detail=values.get(ENGINE_IDENTITY, ""),
    )
    engines = build_engines([] if engine_name == "pymupdf" else [engine_config])

    policy = CurationPolicy(
        engine_name=engine_name,
        routing=_routing_policy(values),
        language=LanguagePolicy(min_confidence=float(values.get("FINEPDFS_LANGUAGE_CONFIDENCE", "0.65"))),
        publish=PublishPolicy(
            min_characters=int(values.get("FINEPDFS_MIN_CHARACTERS", "200")),
            required_languages=_languages(values),
        ),
    )
    probe = PymupdfTextLayerProbe()
    return Runtime(
        corpus_dir=corpus_dir,
        out_dir=out_dir,
        policy=policy,
        extraction=ExtractionStep(probe, engines, policy),
        curation=CurationStep(build_language_identifier(language_config), policy),
        similarity_threshold=float(values.get("FINEPDFS_SIMILARITY_THRESHOLD", "0.8")),
    )


def _languages(values: Mapping[str, str]) -> tuple[str, ...]:
    """FINEPDFS_LANGUAGES is a comma-separated allow-list; empty means no restriction."""
    return tuple(part.strip() for part in values.get("FINEPDFS_LANGUAGES", "").split(",") if part.strip())


def _routing_policy(values: Mapping[str, str]):
    from finepdfs.domain.ocr import OcrRoutingPolicy

    return OcrRoutingPolicy(
        min_characters_per_page=float(values.get("FINEPDFS_MIN_CHARACTERS_PER_PAGE", "200")),
        min_text_coverage=float(values.get("FINEPDFS_MIN_TEXT_COVERAGE", "0.5")),
        max_garbled_ratio=float(values.get("FINEPDFS_MAX_GARBLED_RATIO", "0.05")),
    )
