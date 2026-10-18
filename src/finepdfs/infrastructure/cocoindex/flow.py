"""The CocoIndex flow: read every document, then decide the corpus in one pass.

Reading is the expensive work, so it is memoised per document and engine (ADR-0005). Deciding what the
corpus contains is neither expensive nor local: cleaning, language and the publish test need one
document, but the near-duplicate decision needs every document. So the flow runs in two passes -- a
concurrent reading pass, whose results are handed to a single deciding pass in memory -- and every
output is declared as target state rather than written by side effect (ADR-0006).
"""

from __future__ import annotations

import logging
import os
import pathlib
from dataclasses import dataclass

import cocoindex as coco
from cocoindex.connectors import localfs

from finepdfs.application.curate import CurationOutcome, ExtractionResult, RoutingPlan
from finepdfs.domain.dedup import (
    DedupPlan,
    DuplicateDecision,
    PublishedDocumentView,
    exact_duplicate_keepers,
    plan_deduplication,
)
from finepdfs.domain.document import Document, ExtractedDocument, content_digest
from finepdfs.domain.publishing import PageRecord
from finepdfs.infrastructure.cocoindex.runtime import Runtime, runtime_from_environment
from finepdfs.infrastructure.compute.providers import leased_environment
from finepdfs.infrastructure.corpus.index import CorpusDocument
from finepdfs.infrastructure.corpus.manifest import pdf_path
from finepdfs.infrastructure.publishing.disk import DUPLICATES_REPORT, KEPT_REPORT, PublishedCorpus

logger = logging.getLogger(__name__)

_app_runtime: Runtime | None = None


def configure_runtime(runtime: Runtime | None) -> None:
    """Point the flow at one runtime. Called once per run from the lifespan."""
    global _app_runtime
    _app_runtime = runtime


def active_runtime() -> Runtime:
    if _app_runtime is None:
        raise RuntimeError("the flow has no runtime; it is configured when the app environment starts")
    return _app_runtime


@coco.lifespan
def coco_lifespan(builder: coco.EnvironmentBuilder):
    """Where the database lives, which GPU this run rents, and how the run is wired.

    The lease wraps the whole update, so a rented worker lives exactly as long as the run and is given
    back even when the run fails. Its endpoint is exported because the flow is configured by the
    environment alone: `cocoindex update main.py` takes no arguments of its own. Both the worker and
    the environment the lease published are restored on the way out, so a failed run leaves no process
    still pointing at a worker it has given back. A stop command that itself fails cannot be undone,
    and is not passed over either: the provider reports the leak beside the run's own error (ADR-0007).
    """
    builder.settings.db_path = pathlib.Path(os.environ.get("FINEPDFS_DB", "cocoindex.db"))
    with leased_environment(os.environ):
        if _app_runtime is None:
            # A caller that configured a runtime itself (a dry run, a test) keeps it.
            configure_runtime(runtime_from_environment())
        try:
            yield
        finally:
            configure_runtime(None)


@coco.fn(memo=True)
async def extract_with_engine(digest: str, engine_name: str, engine_identity: str) -> ExtractedDocument:
    """Read one document with one engine.

    Memoised on the content digest, the engine name and the engine identity, and on nothing else: not
    on where the corpus happens to live, so moving a corpus re-reads nothing (ADR-0003), and not on
    where the document came from. The identity is an argument for the key's sake alone -- a different
    model, base URL, page DPI or tool version changes it, so memoised text cannot outlive the engine
    that produced it (ADR-0005). The bytes are checked against the digest they are named after, so a
    file that no longer holds what its name says fails visibly instead of serving stale text.
    """
    runtime = active_runtime()
    path = pdf_path(runtime.corpus_dir, digest)
    content = path.read_bytes()
    if content_digest(content) != digest:
        raise ValueError(f"{path} does not hold the bytes its digest names")
    document = Document(name=str(path), content=content)
    return runtime.extraction.extract_with(document, engine_name)


@dataclass(frozen=True, slots=True)
class ReadingRequest:
    """One document to read, and the document it shares its bytes with (usually itself)."""

    document: CorpusDocument
    keeper: str


@dataclass(frozen=True, slots=True)
class DocumentReading:
    """One document after the reading pass: what it is, how it was read, and what went wrong."""

    document: CorpusDocument
    plan: RoutingPlan | None = None
    extracted: ExtractedDocument | None = None
    exact_duplicate_of: str = ""
    failure: str = ""


@coco.fn
async def read_document(request: ReadingRequest) -> DocumentReading:
    """Route and read one document. A bad file, or an engine that is down, is that document's problem."""
    document = request.document
    if request.keeper != document.url:
        return DocumentReading(document=document, exact_duplicate_of=request.keeper)
    runtime = active_runtime()
    try:
        content = document.path.read_bytes()
        if content_digest(content) != document.digest:
            raise ValueError(f"{document.path} does not hold the bytes its digest names")
        plan = runtime.extraction.route(content)
        engine = runtime.engines[plan.engine_name]
        # The mount path is part of CocoIndex's memo scope, not just the arguments below: naming the
        # component by URL would put a survivor promoted to keeper under a path that never ran, so the
        # corpus would pay for the same bytes twice. The digest is the identity ADR-0005 keys
        # Extraction on, so it names the component too. A database written before this path existed
        # misses once and re-reads each document before memoising it again.
        extracted = await coco.use_mount(
            coco.component_subpath("extract", document.digest),
            extract_with_engine,
            document.digest,
            plan.engine_name,
            str(engine.identity),
        )
    except Exception as error:  # one unreadable document must not end the run
        logger.warning("could not read %s: %s", document.url, error)
        return DocumentReading(document=document, plan=None, extracted=None, failure=_describe(error))
    return DocumentReading(document=document, plan=plan, extracted=extracted)


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


@coco.fn
async def publish_corpus(readings: tuple[DocumentReading, ...], similar_at: float) -> None:
    """Decide the whole corpus at once: what is published, what duplicates what, and the report."""
    runtime = active_runtime()
    published = runtime.published()

    curated: dict[str, tuple[CorpusDocument, CurationOutcome]] = {}
    for item in readings:
        if item.extracted is None or item.exact_duplicate_of:
            continue
        url = item.document.url
        curated[url] = (item.document, runtime.curation.curate(_extraction(item), url, item.document.digest))

    plan = plan_deduplication(
        [
            PublishedDocumentView(url=document.url, digest=document.digest, text=outcome.markdown)
            for document, outcome in curated.values()
            if outcome.published
        ],
        similarity_threshold=similar_at,
    )
    # Reuse the digest keepers that skipped Extraction; they also explain every exact exclusion.
    exact = [
        DuplicateDecision(
            url=item.document.url, kind="exact", duplicate_of=item.exact_duplicate_of, similarity=1.0
        )
        for item in readings
        if item.exact_duplicate_of
    ]
    plan = DedupPlan(
        keepers=plan.keepers,
        duplicates=tuple(sorted([*plan.duplicates, *exact], key=lambda decision: decision.url)),
    )
    duplicates = plan.duplicates_by_url()

    for item in readings:
        _declare(published, item, curated, duplicates)

    localfs.declare_file(
        published.dedup_report_path(KEPT_REPORT), published.kept_payload(plan), create_parent_dirs=True
    )
    localfs.declare_file(
        published.dedup_report_path(DUPLICATES_REPORT),
        published.duplicates_payload(plan),
        create_parent_dirs=True,
    )


def _extraction(item: DocumentReading) -> ExtractionResult:
    assert item.extracted is not None and item.plan is not None
    return ExtractionResult(document=item.extracted, plan=item.plan)


def _declare(
    published: PublishedCorpus,
    item: DocumentReading,
    curated: dict[str, tuple[CorpusDocument, CurationOutcome]],
    duplicates: dict[str, DuplicateDecision],
) -> None:
    """Declare one document's outputs: its text and record, or the reason it is not published."""
    document = item.document
    decision = duplicates.get(document.url)
    if decision is not None and decision.kind == "exact":
        _declare_exclusion(
            published, document, f"identical bytes to {decision.duplicate_of}", "exact-duplicate"
        )
        return
    if item.extracted is None:
        _declare_exclusion(published, document, item.failure, "failed")
        return

    outcome = curated[document.url][1]
    if not outcome.published or outcome.record is None:
        _declare_exclusion(published, document, outcome.reason, outcome.status)
        return

    if decision is not None:
        reason = f"near-duplicate of {decision.duplicate_of} (similarity {decision.similarity:.3f})"
        _declare_exclusion(published, document, reason, "near-duplicate")
        return

    localfs.declare_file(published.doc_path(document.key), outcome.markdown, create_parent_dirs=True)
    localfs.declare_file(
        published.record_path(document.key), published.record_payload(outcome.record), create_parent_dirs=True
    )
    # Only a published Document declares page records, so an exclusion, a duplicate or a Crawl Record
    # that left the manifest stops declaring them and the target state retracts the old file.
    localfs.declare_file(
        published.pages_path(document.key),
        published.pages_payload(_page_records(document, outcome)),
        create_parent_dirs=True,
    )


def _page_records(document: CorpusDocument, outcome: CurationOutcome) -> tuple[PageRecord, ...]:
    """The accepted Page Texts, under the source identity their declared file is named after."""
    assert outcome.record is not None
    return tuple(
        PageRecord(
            doc_id=document.key,
            source_url=document.url,
            digest=document.digest,
            page_number=page.number,
            text=page.text,
            engine=outcome.record.engine,
        )
        for page in outcome.pages
    )


def _declare_exclusion(published: PublishedCorpus, document: CorpusDocument, reason: str, kind: str) -> None:
    localfs.declare_file(
        published.exclusion_path(document.key),
        published.exclusion_payload(document.url, reason, kind),
        create_parent_dirs=True,
    )


@coco.fn
async def app_main() -> None:
    """Read every document concurrently, then decide the corpus in one pass."""
    runtime = active_runtime()
    documents = runtime.corpus().documents()
    keepers = exact_duplicate_keepers({document.url: document.digest for document in documents})
    requests = tuple(ReadingRequest(document=document, keeper=keepers[document.url]) for document in documents)
    readings = await coco.map(read_document, requests)
    await coco.use_mount(
        coco.component_subpath("publish"), publish_corpus, tuple(readings), runtime.similarity_threshold
    )


def build_app() -> coco.App:
    return coco.App(coco.AppConfig(name="FinePDFs"), app_main)
