"""Building the engines a run was asked for, from a plain configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from finepdfs.domain.ports import OcrEngine
from finepdfs.infrastructure.engines.concurrency import DEFAULT_ENGINE_CONCURRENCY
from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextEngine
from finepdfs.infrastructure.engines.stable_identity import StableIdentity
from finepdfs.infrastructure.engines.subprocess_support import SubprocessRunner

NATIVE_ENGINE = "pymupdf"
DOCLING_ENGINE = "docling"
PADDLEOCR_VL_ENGINE = "paddleocr-vl"
MINERU_ENGINE = "mineru"
KNOWN_ENGINES = (NATIVE_ENGINE, DOCLING_ENGINE, PADDLEOCR_VL_ENGINE, MINERU_ENGINE)

# The engines whose identity has to carry the page DPI they render at. ADR-0005 makes page DPI an
# identity input, so ``_deployment_detail`` appends ``@dpi{dpi}`` to a pinned detail for these engines
# and no others. An engine that renders at a DPI -- and so carries one in its own detail -- belongs in
# this tuple; the registry test fails until it is added, because a pinned ``--engine-detail`` would
# otherwise silently drop the DPI and a DPI change would be served the other DPI's text from the memo.
DPI_CARRYING_ENGINES = (PADDLEOCR_VL_ENGINE,)


class UnknownEngine(ValueError):
    """Raised when a run names an engine that does not exist."""


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """How to reach one engine: a local command, or an HTTP endpoint of a model server."""

    name: str = NATIVE_ENGINE
    command: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    page_dpi: int = 200
    # How many engine calls this run may keep in flight at once (ADR-0010 step 3): one page request for
    # a served engine, one command for a local one. It sizes the bound on the one Engine instance the run
    # shares across Documents, so it says something about the worker, never about render capacity.
    concurrency: int = DEFAULT_ENGINE_CONCURRENCY
    timeout_seconds: float = 300.0
    work_dir: Path | None = None
    detail: str = ""
    # The detail half of the engine's identity, when the address it is reached at is not stable.
    #
    # A rented GPU answers on a new host every run, so its address cannot be the memo key without
    # re-reading the whole corpus each time (ADR-0005). Empty means the engine names itself, which is
    # right for anything reached at an address that does not move. This is what ``StableIdentity`` and
    # ``EngineIdentity`` both call ``detail``: the name is the engine's, the detail is the deployment's.


def build_engine(config: EngineConfig) -> OcrEngine:
    """One engine, whichever kind it is. Imported lazily so unused engines cost nothing."""
    engine = _build_engine(config)
    detail = _deployment_detail(config)
    if detail:
        return StableIdentity(engine, detail)
    return engine


def _deployment_detail(config: EngineConfig) -> str:
    """The configured deployment detail, carrying whatever the run's engine renders at.

    ``StableIdentity`` replaces the adapter's detail wholesale -- and with it any page DPI the adapter
    put there. ADR-0005 makes the page DPI an identity input, so the name a provider pins has to carry
    it too, or changing ``FINEPDFS_ENGINE_DPI`` on a leased worker would be served the other DPI's text.
    Only the engines in ``DPI_CARRYING_ENGINES`` render at a DPI and carry one; a new engine that does
    has to join that tuple and be handled here, which the registry test pins.
    """
    if not config.detail:
        return ""
    if config.name in DPI_CARRYING_ENGINES:
        from finepdfs.infrastructure.engines.paddleocr_vl import paddleocr_detail

        return paddleocr_detail(config.detail, config.page_dpi)
    return config.detail


def _build_engine(config: EngineConfig) -> OcrEngine:
    if config.name == NATIVE_ENGINE:
        return PymupdfTextEngine()
    if config.name == DOCLING_ENGINE:
        from finepdfs.infrastructure.engines.docling_engine import DoclingEngine

        return DoclingEngine(
            command=config.command or "docling",
            work_dir=config.work_dir,
            runner=SubprocessRunner(config.timeout_seconds),
        )
    if config.name == PADDLEOCR_VL_ENGINE:
        from finepdfs.infrastructure.engines.paddleocr_vl import PaddleOcrVlCliEngine, PaddleOcrVlEngine

        if config.command:
            return PaddleOcrVlCliEngine(
                command=config.command,
                work_dir=config.work_dir,
                dpi=config.page_dpi,
                concurrency=config.concurrency,
                runner=SubprocessRunner(config.timeout_seconds),
            )
        from finepdfs.infrastructure.engines.vlm_chat import ChatEndpoint, VlmChatTransport

        endpoint = ChatEndpoint(
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
            timeout_seconds=config.timeout_seconds,
        )
        return PaddleOcrVlEngine(
            transport=VlmChatTransport(endpoint), dpi=config.page_dpi, concurrency=config.concurrency
        )
    if config.name == MINERU_ENGINE:
        from finepdfs.infrastructure.engines.mineru import MineruEngine, MineruHttpBackend, MineruSubprocessBackend

        if config.base_url:
            backend = MineruHttpBackend(
                base_url=config.base_url, api_key=config.api_key, timeout_seconds=config.timeout_seconds
            )
        else:
            backend = MineruSubprocessBackend(
                command=config.command or "mineru",
                work_dir=config.work_dir,
                runner=SubprocessRunner(config.timeout_seconds),
            )
        return MineruEngine(backend=backend)
    raise UnknownEngine(f"unknown engine {config.name!r}; known engines are {', '.join(KNOWN_ENGINES)}")


def build_engines(configs: list[EngineConfig]) -> dict[str, OcrEngine]:
    """Every configured engine, keyed by the name a run refers to it by, plus the native one."""
    engines = {NATIVE_ENGINE: PymupdfTextEngine()}
    for config in configs:
        if config.name in engines:
            continue
        engines[config.name] = build_engine(config)
    return engines
