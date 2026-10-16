"""The CocoIndex flow: one component per corpus document.

The expensive unit is "read this document with this engine", and it is memoised on exactly those two
things. Everything that changes often -- cleaning rules, language thresholds, publish minimums --
lives outside the memoised unit, so changing a threshold re-runs the cheap half and never re-runs an
engine. Changing the engine name changes the memo key, so the document is read again by the new engine.
See ADR-0003.
"""

from __future__ import annotations

import logging
import os
import pathlib

import cocoindex as coco
from cocoindex.connectors import localfs

from finepdfs.application.curate import ExtractionResult
from finepdfs.domain.dedup import exact_duplicate_keepers
from finepdfs.domain.document import Document, ExtractedDocument
from finepdfs.infrastructure.cocoindex.runtime import Runtime, runtime_from_environment
from finepdfs.infrastructure.corpus.index import CorpusDocument

logger = logging.getLogger(__name__)

_app_runtime: Runtime | None = None


def configure_runtime(runtime: Runtime) -> None:
    """Point the flow at one runtime. Called once per run from the lifespan."""
    global _app_runtime
    _app_runtime = runtime


def active_runtime() -> Runtime:
    if _app_runtime is None:
        raise RuntimeError("the flow has no runtime; it is configured when the app environment starts")
    return _app_runtime


@coco.lifespan
def coco_lifespan(builder: coco.EnvironmentBuilder):
    """Where the database lives, and how this run is wired."""
    builder.settings.db_path = pathlib.Path(os.environ.get("FINEPDFS_DB", "cocoindex.db"))
    configure_runtime(runtime_from_environment())
    yield
    configure_runtime(None)


@coco.fn(memo=True)
async def extract_with_engine(pdf_path: pathlib.Path, engine_name: str) -> ExtractedDocument:
    """Read one document with one engine. Memoised on the path and the engine name."""
    runtime = active_runtime()
    document = Document(name=str(pdf_path), content=pdf_path.read_bytes())
    return runtime.extraction.extract_with(document, engine_name)


@coco.fn
async def process_document(document: CorpusDocument, exact_keepers: dict[str, str]) -> None:
    """Curate one document and declare what it produced."""
    runtime = active_runtime()
    published = runtime.published()

    keeper = exact_keepers.get(document.url)
    if keeper is not None and keeper != document.url:
        localfs.declare_file(
            published.exclusion_path(document.key),
            published.exclusion_payload(document.url, f"identical bytes to {keeper}", "exact-duplicate"),
            create_parent_dirs=True,
        )
        return

    try:
        plan = runtime.extraction.route(document.path.read_bytes())
    except Exception as error:
        _declare_failure(published, document, error)
        return

    try:
        extracted = await coco.use_mount(
            coco.component_subpath("extract"), extract_with_engine, document.path, plan.engine_name
        )
    except Exception as error:  # a bad file or an engine that is down is one document's problem
        _declare_failure(published, document, error)
        return

    outcome = runtime.curation.curate(ExtractionResult(document=extracted, plan=plan), document.url, document.digest)
    if outcome.published and outcome.record is not None:
        localfs.declare_file(published.doc_path(document.key), outcome.markdown, create_parent_dirs=True)
        localfs.declare_file(
            published.record_path(document.key), published.record_payload(outcome.record), create_parent_dirs=True
        )
    else:
        localfs.declare_file(
            published.exclusion_path(document.key),
            published.exclusion_payload(document.url, outcome.reason, outcome.status),
            create_parent_dirs=True,
        )


def _declare_failure(published, document: CorpusDocument, error: Exception) -> None:
    logger.warning("could not process %s: %s", document.url, error)
    localfs.declare_file(
        published.exclusion_path(document.key),
        published.exclusion_payload(document.url, f"{type(error).__name__}: {error}", "failed"),
        create_parent_dirs=True,
    )


@coco.fn
async def app_main() -> None:
    """One component per corpus document, in a stable order."""
    documents = active_runtime().corpus().documents()
    exact_keepers = exact_duplicate_keepers({document.url: document.digest for document in documents})
    items = [(document.key, document) for document in documents]
    await coco.mount_each(process_document, items, exact_keepers)


def build_app() -> coco.App:
    return coco.App(coco.AppConfig(name="FinePDFs"), app_main)
