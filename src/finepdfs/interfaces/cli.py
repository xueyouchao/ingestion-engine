"""The command line: fetch a real Common Crawl corpus, inspect it, curate and search it."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from finepdfs.application.search import reindex_search, search_pdfs
from finepdfs.domain.crawl import CrawlQuery
from finepdfs.domain.dedup import PublishedDocumentView, exact_duplicate_keepers, plan_deduplication
from finepdfs.domain.document import Document
from finepdfs.domain.search import DEFAULT_TOP_K, SearchFilters
from finepdfs.infrastructure.cocoindex.runtime import Runtime, runtime_from_environment
from finepdfs.infrastructure.commoncrawl.source import CommonCrawlSource
from finepdfs.infrastructure.compute.endpoint import (
    ENGINE_API_KEY,
    ENGINE_BASE_URL,
    ENGINE_DETAIL,
    ENGINE_MODEL,
)
from finepdfs.infrastructure.compute.providers import (
    GPU_PROVIDER,
    GPU_START_COMMAND,
    GPU_STOP_COMMAND,
    PROVIDERS,
    GpuStartFailed,
    GpuStopFailed,
    MisconfiguredGpuProvider,
    UnknownGpuProvider,
    leased_environment,
)
from finepdfs.infrastructure.corpus.fetch import fetch_corpus, restore_corpus
from finepdfs.infrastructure.corpus.index import CorpusIndex
from finepdfs.infrastructure.corpus.manifest import MANIFEST_NAME, CorpusManifest
from finepdfs.infrastructure.engines.registry import KNOWN_ENGINES
from finepdfs.infrastructure.publishing.disk import PublishedCorpus
from finepdfs.infrastructure.search.sqlite_fts import SqliteFtsSearchIndex
from finepdfs.interfaces.summary import format_run_summary

DEFAULT_CORPUS = Path("corpus")
DEFAULT_OUT = Path("out")
SEARCH_INDEX_NAME = "search.db"
DEFAULT_SEARCH_INDEX = DEFAULT_OUT / SEARCH_INDEX_NAME
SEARCH_INDEX_VARIABLE = "FINEPDFS_SEARCH_INDEX"

# Flag name to environment variable. "engine_command" is deliberately not called "command": a
# subparser argument with that name would overwrite the name of the chosen subcommand. The names the GPU
# seam introduced are read from their owners -- compute.endpoint writes the FINEPDFS_ENGINE_* four and
# compute.providers owns the GPU_* -- so a rename is one edit there, not one here as well. The older
# names below have no constant yet and are spelled where they are read.
ENVIRONMENT_FOR_ENGINE = {
    "corpus": "FINEPDFS_CORPUS",
    "out": "FINEPDFS_OUT",
    "engine": "FINEPDFS_ENGINE",
    "base_url": ENGINE_BASE_URL,
    "model": ENGINE_MODEL,
    "api_key": ENGINE_API_KEY,
    "engine_command": "FINEPDFS_ENGINE_COMMAND",
    "engine_detail": ENGINE_DETAIL,
    "dpi": "FINEPDFS_ENGINE_DPI",
    "similarity_threshold": "FINEPDFS_SIMILARITY_THRESHOLD",
    "gpu_provider": GPU_PROVIDER,
    "gpu_start_command": GPU_START_COMMAND,
    "gpu_stop_command": GPU_STOP_COMMAND,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finepdfs", description="FinePDFs-style PDF curation on CocoIndex")
    parser.add_argument("--verbose", action="store_true", help="log progress to stderr")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch-corpus", help="fetch real PDFs from Common Crawl into a corpus directory")
    fetch.add_argument("--domain", required=True, help="domain to take PDFs from, for example noaa.gov")
    fetch.add_argument("--crawl", default=None, help="crawl id; default is the newest full crawl")
    fetch.add_argument("--limit", type=int, default=25, help="how many index records to ask for")
    fetch.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)

    restore = commands.add_parser("restore-corpus", help="restore the exact PDFs a pinned manifest names")
    restore.add_argument("--manifest", type=Path, required=True, help="pinned manifest to replay")
    restore.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)

    inspect = commands.add_parser("inspect", help="summarise a corpus directory")
    inspect.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)

    dry_run = commands.add_parser("dry-run", help="curate the corpus in process and print what would happen")
    _add_engine_arguments(dry_run, out=False)

    run = commands.add_parser("run", help="run the incremental CocoIndex pipeline")
    _add_engine_arguments(run, out=True)

    # The local full-text fallback (ADR-0008): rebuild the index from the declared chunk files, then
    # read the same contract the browser and the agent use. Two commands, one backend.
    index_search = commands.add_parser(
        "index-search", help="rebuild the local lexical index from the declared chunk files"
    )
    index_search.add_argument(
        "--out", type=Path, default=None, help="the published output directory the flow declared"
    )
    index_search.add_argument(
        "--index", type=Path, default=None, help=f"the index to write; default is <out>/{SEARCH_INDEX_NAME}"
    )

    search = commands.add_parser("search", help="search accepted evidence in the local lexical index")
    search.add_argument(
        "--index", type=Path, default=None, help=f"the index to read; default is {DEFAULT_SEARCH_INDEX}"
    )
    search.add_argument("--top-k", dest="top_k", type=int, default=DEFAULT_TOP_K, help="maximum hits to return")
    search.add_argument("--language", default=None, help="only chunks whose language tag is this")
    search.add_argument("--engine", default=None, help="only chunks produced by this Engine Identity")
    search.add_argument("--doc-id", dest="doc_id", default=None, help="only chunks of this source identity")
    search.add_argument("--json", action="store_true", help="print the shared search contract as JSON")
    search.add_argument("query", nargs="+", help="words to look for")

    return parser


def _add_engine_arguments(parser: argparse.ArgumentParser, *, out: bool) -> None:
    # Every flag suppresses its own default on purpose: a flag that was not typed must not appear in
    # the namespace, or _runtime_from_args would export a default over the FINEPDFS_* the user set.
    parser.add_argument("--corpus", type=Path, default=argparse.SUPPRESS)
    if out:
        parser.add_argument("--out", type=Path, default=argparse.SUPPRESS)
        parser.add_argument(
            "--allow-empty",
            action="store_true",
            help="publish an empty target state on purpose; without this, a run refuses to delete every output",
        )
    parser.add_argument(
        "--engine",
        default=argparse.SUPPRESS,
        choices=list(KNOWN_ENGINES),
        help="the engine that reads documents whose text layer is unusable",
    )
    parser.add_argument(
        "--base-url", default=argparse.SUPPRESS, help="OpenAI-compatible model server, for paddleocr-vl and mineru"
    )
    parser.add_argument("--model", default=argparse.SUPPRESS, help="model name to ask the server for")
    parser.add_argument("--api-key", default=argparse.SUPPRESS, help="bearer token for the model server")
    parser.add_argument(
        "--engine-command",
        dest="engine_command",
        default=argparse.SUPPRESS,
        help="local command, for docling, mineru and paddleocr-vl",
    )
    parser.add_argument(
        "--dpi", type=int, default=argparse.SUPPRESS, help="resolution used to render pages for a vision model"
    )
    parser.add_argument(
        "--similarity-threshold",
        type=float,
        default=argparse.SUPPRESS,
        help="fingerprint similarity above which one document is a near-duplicate of another",
    )
    parser.add_argument(
        "--engine-detail",
        dest="engine_detail",
        default=argparse.SUPPRESS,
        help="the deployment detail in the engine's memo key, for a worker whose address changes every run",
    )
    parser.add_argument(
        "--gpu-provider",
        dest="gpu_provider",
        default=argparse.SUPPRESS,
        choices=list(PROVIDERS),
        help="where a GPU engine runs: " + ", ".join(PROVIDERS),
    )
    parser.add_argument(
        "--gpu-start-command",
        dest="gpu_start_command",
        default=argparse.SUPPRESS,
        help="with --gpu-provider command: starts a worker and prints its endpoint",
    )
    parser.add_argument(
        "--gpu-stop-command",
        dest="gpu_stop_command",
        default=argparse.SUPPRESS,
        help="with --gpu-provider command: gives the worker back",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    try:
        return _dispatch(args)
    except (UnknownGpuProvider, MisconfiguredGpuProvider, GpuStartFailed, GpuStopFailed) as error:
        # A run that cannot lease its GPU is an operator problem, not a crash: say so and exit 2, the
        # same way an empty corpus is refused rather than run. A worker the stop command could not give
        # back is the same kind of message for the same reason -- it is still billing (ADR-0007).
        # Deliberately not CommandFailed: an engine whose own command fails is a run that started, and
        # its traceback names the engine, not the provider.
        print(error, file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "fetch-corpus":
        return _fetch_corpus(args)
    if args.command == "restore-corpus":
        return _restore_corpus(args)
    if args.command == "inspect":
        return _inspect(args)
    if args.command == "dry-run":
        return _dry_run(args)
    if args.command == "run":
        return _run(args)
    if args.command == "index-search":
        return _index_search(args)
    if args.command == "search":
        return _search(args)
    raise AssertionError(f"unhandled command {args.command}")


def _search_index_path(args: argparse.Namespace) -> Path:
    """Where the fallback index lives: the flag, else the output directory's own default, else the env."""
    if args.index is not None:
        return args.index
    out_dir = getattr(args, "out", None)
    if out_dir is not None:
        return out_dir / SEARCH_INDEX_NAME
    return Path(os.environ.get(SEARCH_INDEX_VARIABLE, DEFAULT_SEARCH_INDEX))


def _index_search(args: argparse.Namespace) -> int:
    """Rebuild the local index from the declared chunk files: store the current set, delete the rest."""
    out_dir = args.out if args.out is not None else Path(os.environ.get("FINEPDFS_OUT", DEFAULT_OUT))
    if not out_dir.is_dir():
        print(f"no published output at {out_dir}; run the pipeline first", file=sys.stderr)
        return 2
    index_path = _search_index_path(args)
    report = reindex_search(SqliteFtsSearchIndex(index_path), PublishedCorpus(out_dir).read_chunks())
    print(f"indexed {report.upserted} of {report.chunks} chunks from {report.documents} documents into {index_path}")
    if report.removed:
        print(f"  retracted {report.removed} chunks that are no longer declared")
    for failure in report.failures:
        print(f"  failed: {failure.chunk_id} ({failure.reason})", file=sys.stderr)
    return 0 if report.ok else 1


def _search(args: argparse.Namespace) -> int:
    """Read the shared contract from the terminal; the browser and the agent call the same reader."""
    index_path = _search_index_path(args)
    if not index_path.exists():
        print(f"no search index at {index_path}; run finepdfs index-search first", file=sys.stderr)
        return 2
    query = " ".join(args.query)
    filters = SearchFilters(language=args.language, engine=args.engine, doc_id=args.doc_id)
    results = search_pdfs(SqliteFtsSearchIndex(index_path), query, filters, args.top_k)

    if args.json:
        print(json.dumps(results.as_json(), indent=2))
        return 0
    if not results.hits:
        print(f"no matches for {query!r} in {index_path}")
        return 0
    for rank, hit in enumerate(results.hits, start=1):
        # A hit is citable on one line: rank, score, source identity, original page and chunk.
        print(f"{rank:>2}. {hit.score:>8.3f}  {hit.doc_id}  p.{hit.page_number}  {hit.chunk_id}")
        print(f"    {hit.source_url}")
        print(f"    {' '.join(hit.snippet.split())}")
    return 0


def _fetch_corpus(args: argparse.Namespace) -> int:
    corpus_dir = _runtime_from_args(args).corpus_dir
    source = CommonCrawlSource()
    crawl_id = args.crawl or source.latest_crawl_id()
    query = CrawlQuery(crawl_id=crawl_id, domain=args.domain, limit=args.limit)
    print(f"fetching up to {args.limit} PDFs for {args.domain} from {crawl_id} into {corpus_dir}")

    report = fetch_corpus(source, query, corpus_dir)

    print(report.summary())
    for failure in report.failures[:5]:
        print(f"  failed: {failure.url} ({failure.reason})")
    return 1 if report.fetched == 0 and report.failed else 0


def _restore_corpus(args: argparse.Namespace) -> int:
    """Replay a committed manifest; a pinned record that cannot be restored is a visible failure."""
    corpus_dir = _runtime_from_args(args).corpus_dir
    pinned = CorpusManifest.load(args.manifest)
    if not pinned.entries:
        print(f"no pinned manifest at {args.manifest} (or it lists no entries)", file=sys.stderr)
        return 2

    print(f"restoring {len(pinned.entries)} pinned PDFs from {pinned.crawl_id} into {corpus_dir}")
    report = restore_corpus(CommonCrawlSource(), pinned, corpus_dir)

    print(report.summary())
    for failure in report.failures:
        print(f"  failed: {failure.url} ({failure.reason})")
    return 1 if report.failed else 0


def _inspect(args: argparse.Namespace) -> int:
    corpus_dir = _runtime_from_args(args).corpus_dir
    manifest_path = corpus_dir / MANIFEST_NAME
    manifest = CorpusManifest.load(manifest_path)
    if not manifest.entries:
        print(f"no corpus at {corpus_dir} (looked for {manifest_path})", file=sys.stderr)
        return 2

    total_bytes = sum(entry.bytes for entry in manifest.entries)
    print(f"corpus: {corpus_dir}")
    print(f"  crawl: {manifest.crawl_id}  domain: {manifest.domain}")
    print(f"  documents: {len(manifest.entries)}  distinct content: {len(manifest.digests)}")
    print(f"  bytes: {total_bytes}  mean: {total_bytes // max(len(manifest.entries), 1)}")
    for entry in manifest.entries[:5]:
        print(f"  {entry.digest[:12]}  {entry.bytes:>8}  {entry.url}")
    if len(manifest.entries) > 5:
        print(f"  ... and {len(manifest.entries) - 5} more")
    return 0


def _export_flags(args: argparse.Namespace) -> None:
    """The flags a run was given, expressed as the environment the flow reads."""
    for attribute, variable in ENVIRONMENT_FOR_ENGINE.items():
        value = getattr(args, attribute, None)
        if value is not None:
            os.environ[variable] = str(value)


def _runtime_from_args(args: argparse.Namespace) -> Runtime:
    _export_flags(args)
    return runtime_from_environment()


def _dry_run(args: argparse.Namespace) -> int:
    """Curate in process, on whatever GPU this run was told to rent.

    A dry run does not go through the CocoIndex flow, so the flow's lifespan cannot lease for it: it
    leases here, exports the endpoint, and only then builds the runtime that holds the engine.
    """
    _export_flags(args)
    with leased_environment(os.environ):
        return _dry_run_in_process(runtime_from_environment())


def _dry_run_in_process(runtime: Runtime) -> int:
    index = CorpusIndex(runtime.corpus_dir)
    documents = index.documents()
    if not documents:
        print(f"no corpus at {runtime.corpus_dir}", file=sys.stderr)
        return 2

    keepers = exact_duplicate_keepers({document.url: document.digest for document in documents})
    use_case = runtime.curator()

    curated = {}
    for document in documents:
        if keepers[document.url] != document.url:
            continue
        outcome = use_case.run(Document(name=document.url, content=document.path.read_bytes()))
        curated[document.url] = (document, outcome)

    plan = plan_deduplication(
        [
            PublishedDocumentView(url=document.url, digest=document.digest, text=outcome.markdown)
            for document, outcome in curated.values()
            if outcome.published
        ],
        similarity_threshold=runtime.similarity_threshold,
    )
    near_duplicate = {decision.url: decision for decision in plan.duplicates if decision.kind == "near"}

    print(f"{'status':<14} {'engine':<22} {'language':<9} {'pages':>5} {'chars':>7}  {'url':<45}  reason")
    for document in documents:
        keeper = keepers[document.url]
        if keeper != document.url:
            _print_row("exact-duplicate", "-", "-", 0, 0, document.url, f"identical bytes to {keeper}")
            continue
        outcome = curated[document.url][1]
        record = outcome.record
        engine = record.engine if record else "-"
        language = record.language if record else "-"
        pages = record.page_count if record else 0
        characters = record.character_count if record else 0
        decision = near_duplicate.get(document.url)
        if decision is not None:
            _print_row(
                "near-duplicate",
                engine,
                language,
                pages,
                characters,
                document.url,
                f"near-duplicate of {decision.duplicate_of}",
            )
            continue
        _print_row(outcome.status, engine, language, pages, characters, document.url, outcome.reason)

    byte_identical = len(documents) - len(curated)
    print(
        f"{len(plan.keepers)} of {len(documents)} documents would be published "
        f"({len(near_duplicate)} near-duplicates of another, {byte_identical} byte-identical)"
    )
    return 0


def _print_row(
    status: str, engine: str, language: str, pages: int, characters: int, url: str, reason: str
) -> None:
    print(f"{status:<14} {engine:<22} {language:<9} {pages:>5} {characters:>7}  {url:<45}  {reason}")


def _run(args: argparse.Namespace) -> int:
    import cocoindex as coco

    from finepdfs.infrastructure.cocoindex.flow import build_app

    runtime = _runtime_from_args(args)
    documents = CorpusIndex(runtime.corpus_dir).documents()
    if not documents and not getattr(args, "allow_empty", False):
        print(f"no corpus at {runtime.corpus_dir}: refusing to run", file=sys.stderr)
        print("  an empty corpus is an empty target state, so every published file would be deleted", file=sys.stderr)
        print("  fetch one with finepdfs fetch-corpus, or pass --allow-empty to mean it", file=sys.stderr)
        return 2

    with coco.runtime():
        build_app().update_blocking(report_to_stdout=True)

    published = PublishedCorpus(runtime.out_dir)
    records = published.read_records()
    exclusions = published.read_exclusions()
    summary = published.read_summary()
    if summary is not None:
        print(format_run_summary(summary))
    else:
        failed = sum(1 for exclusion in exclusions if exclusion.get("kind") == "failed")
        print(
            f"recorded {len(records)} published documents, {failed} failed documents and "
            f"{len(exclusions) - failed} other exclusions under {runtime.out_dir}"
        )
    return 1 if exclusions and not records else 0


if __name__ == "__main__":
    raise SystemExit(main())
