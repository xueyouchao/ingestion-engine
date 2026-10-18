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
from finepdfs.application.metrics import RunMetrics
from finepdfs.domain.chunks import chunk_page_records
from finepdfs.domain.dedup import (
    DuplicateDecision,
    decide_duplicates,
    exact_duplicate_keepers,
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
    # This body runs only on a memo miss, so this is one actual Extraction and never a memo hit. It
    # is counted after the digest check because a file that no longer matches never reaches an engine.
    runtime.metrics.count_extraction(digest, engine_name, engine_identity)
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
        # Route the keeper and remember the memo key this run is about to ask for. The routing
        # decision is outside the memoised body, so a threshold change can move a document between
        # text layer and OCR and the key moves with it (ADR-0005).
        runtime.metrics.route(needs_ocr=plan.decision.needs_ocr)
        runtime.metrics.request_extraction(document.digest, plan.engine_name, str(engine.identity))
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
    outcomes: dict[str, CurationOutcome] = {}
    for item in readings:
        if item.extracted is None or item.exact_duplicate_of:
            continue
        url = item.document.url
        outcome = runtime.curation.curate(_extraction(item), url, item.document.digest)
        curated[url] = (item.document, outcome)
        outcomes[url] = outcome
    # A reading that failed is a disposition like any other, so the duplicate decision can say what an
    # exact duplicate of it inherits without reading it again.
    for item in readings:
        if item.extracted is None and not item.exact_duplicate_of:
            url = item.document.url
            outcomes[url] = CurationOutcome(status="failed", url=url, reason=item.failure)

    # One owner for both kinds of duplicate (domain/dedup.decide_duplicates), so the dry run and these
    # declarations cannot disagree. An exact duplicate may only name a keeper that is actually
    # published: the digest keeper can itself be a near-duplicate loser, dropped by policy, or failed.
    decision = decide_duplicates(
        {item.document.url: item.document.digest for item in readings},
        outcomes,
        similarity_threshold=similar_at,
    )
    plan = decision.plan
    duplicates = decision.duplicates_by_url()
    inherited = {
        url: (fate.kind, fate.keeper, fate.reason) for url, fate in decision.inherited_by_url().items()
    }

    for item in readings:
        _declare(
            published, item, curated, duplicates, inherited, runtime.metrics, runtime.search_enabled
        )

    localfs.declare_file(
        published.dedup_report_path(KEPT_REPORT), published.kept_payload(plan), create_parent_dirs=True
    )
    localfs.declare_file(
        published.dedup_report_path(DUPLICATES_REPORT),
        published.duplicates_payload(plan),
        create_parent_dirs=True,
    )
    # The summary is target state too, so the flow stays the only writer and a rerun refreshes it in
    # place. It carries no timing: elapsed time is an observation the command wrapper around the run
    # records in `out/last-run.json` (interfaces/run_log.py), never a declared field.
    manifest = runtime.corpus().manifest()
    engine = runtime.engines[runtime.policy.engine_name]
    summary = runtime.metrics.summary(
        crawl_id=manifest.crawl_id,
        domain=manifest.domain,
        crawl_records=len(readings),
        documents=len({item.document.digest for item in readings}),
        engine_name=runtime.policy.engine_name,
        engine_identity=str(engine.identity),
    )
    localfs.declare_file(published.summary_path(), published.summary_payload(summary), create_parent_dirs=True)


def _extraction(item: DocumentReading) -> ExtractionResult:
    assert item.extracted is not None and item.plan is not None
    return ExtractionResult(document=item.extracted, plan=item.plan)


def _declare(
    published: PublishedCorpus,
    item: DocumentReading,
    curated: dict[str, tuple[CorpusDocument, CurationOutcome]],
    duplicates: dict[str, DuplicateDecision],
    inherited: dict[str, tuple[str, str, str]],
    metrics: RunMetrics,
    export_chunks: bool,
) -> None:
    """Declare one document's outputs: its text and record, or the reason it is not published."""
    document = item.document
    decision = duplicates.get(document.url)
    if decision is not None and decision.kind == "exact":
        _declare_exclusion(
            published, metrics, document, f"identical bytes to {decision.duplicate_of}", "exact-duplicate"
        )
        return
    if decision is not None and decision.kind == "near":
        # Reachable before the Extraction check on purpose: an exact duplicate whose digest keeper is a
        # near-duplicate loser follows that keeper's near decision without ever being read.
        reason = f"near-duplicate of {decision.duplicate_of} (similarity {decision.similarity:.3f})"
        _declare_exclusion(published, metrics, document, reason, "near-duplicate")
        return
    fate = inherited.get(document.url)
    if fate is not None:
        kind, keeper, keeper_reason = fate
        _declare_exclusion(
            published,
            metrics,
            document,
            f"identical bytes to {keeper}, which was {kind}: {keeper_reason}",
            kind,
        )
        return
    if item.extracted is None:
        _declare_exclusion(published, metrics, document, item.failure, "failed")
        return

    outcome = curated[document.url][1]
    if not outcome.published or outcome.record is None:
        _declare_exclusion(published, metrics, document, outcome.reason, outcome.status)
        return

    localfs.declare_file(published.doc_path(document.key), outcome.markdown, create_parent_dirs=True)
    localfs.declare_file(
        published.record_path(document.key), published.record_payload(outcome.record), create_parent_dirs=True
    )
    # Only a published Document declares page and chunk records, so an exclusion, a duplicate or a
    # Crawl Record that left the manifest stops declaring them and the target state retracts the old
    # files. The chunks are derived from the accepted pages that were just declared, so a replacement
    # with fewer pages retracts the chunks of the pages that no longer exist.
    pages = _page_records(document, outcome)
    localfs.declare_file(
        published.pages_path(document.key),
        published.pages_payload(pages),
        create_parent_dirs=True,
    )
    if export_chunks:
        # Chunks exist to feed search. A run that did not enable search declares none, so the local
        # index cannot be rebuilt against evidence this run never meant to offer.
        localfs.declare_file(
            published.chunks_path(document.key),
            published.chunks_payload(chunk_page_records(pages, language=outcome.record.language)),
            create_parent_dirs=True,
        )
    metrics.count_published(len(outcome.pages))


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


def _declare_exclusion(
    published: PublishedCorpus, metrics: RunMetrics, document: CorpusDocument, reason: str, kind: str
) -> None:
    localfs.declare_file(
        published.exclusion_path(document.key),
        published.exclusion_payload(document.url, reason, kind),
        create_parent_dirs=True,
    )
    metrics.count_exclusion(kind)


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
