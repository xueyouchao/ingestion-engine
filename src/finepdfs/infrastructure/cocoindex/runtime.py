"""One run's wiring: where the corpus is, where output goes, and which engine rescues scans.

Everything is read from FINEPDFS_* environment variables, because `cocoindex update main.py` takes no
arguments of its own. Nothing here is a domain decision: it only assembles the adapters.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass, field

from finepdfs.application.curate import CurateDocument, CurationPolicy, CurationStep, ExtractionStep
from finepdfs.application.metrics import RunMetrics
from finepdfs.domain.language import LanguagePolicy
from finepdfs.domain.ports import OcrEngine
from finepdfs.domain.publishing import PublishPolicy
from finepdfs.infrastructure.corpus.index import CorpusIndex
from finepdfs.infrastructure.engines.environment import engine_adapters
from finepdfs.infrastructure.engines.registry import NATIVE_ENGINE
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
    # Chunks are a search input, not an ingestion product: the flow declares them only when a run
    # enabled search (FINEPDFS_SEARCH, or the CLI's --search), so a plain curation run declares none
    # and any stale chunk export is retracted.
    search_enabled: bool = False
    # What this run actually did. The flow counts into it at the real seams (routing, the memoised
    # extract body, each declaration) and writes out/run-summary.json from it. Observation, not
    # wiring: it is excluded from equality so two identically wired runtimes still compare equal.
    metrics: RunMetrics = field(default_factory=RunMetrics, compare=False, repr=False)

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
    # Blank means unset here too: a run with no engine named stays on the free text layer.
    engine_name = values.get("FINEPDFS_ENGINE", "").strip() or NATIVE_ENGINE
    language_config = LanguageConfig(
        mode=values.get("FINEPDFS_LANGUAGE", DEFAULT_LANGUAGE_MODE),
        model_path=values.get("FINEPDFS_LANGUAGE_MODEL", ""),
    )

    # One reader owns the engine's environment and the adapters it assembles
    # (infrastructure.engines.environment), and the demo server calls the same one, so the two
    # consumers cannot drift into different engines (ADR-0012).
    adapters = engine_adapters(values, engine_name)

    policy = CurationPolicy(
        engine_name=engine_name,
        routing=adapters.routing,
        language=LanguagePolicy(min_confidence=float(values.get("FINEPDFS_LANGUAGE_CONFIDENCE", "0.65"))),
        publish=PublishPolicy(
            min_characters=int(values.get("FINEPDFS_MIN_CHARACTERS", "200")),
            required_languages=_languages(values),
        ),
    )
    return Runtime(
        corpus_dir=corpus_dir,
        out_dir=out_dir,
        policy=policy,
        extraction=ExtractionStep(adapters.probe, adapters.engines, policy),
        curation=CurationStep(build_language_identifier(language_config), policy),
        similarity_threshold=float(values.get("FINEPDFS_SIMILARITY_THRESHOLD", "0.8")),
        search_enabled=_enabled(values.get("FINEPDFS_SEARCH", "")),
    )


def _enabled(value: str) -> bool:
    """FINEPDFS_SEARCH is a switch: an unset, empty or explicitly false value means search is off."""
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def _languages(values: Mapping[str, str]) -> tuple[str, ...]:
    """FINEPDFS_LANGUAGES is a comma-separated allow-list; empty means no restriction."""
    return tuple(part.strip() for part in values.get("FINEPDFS_LANGUAGES", "").split(",") if part.strip())
