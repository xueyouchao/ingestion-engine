"""The command line: fetch a real Common Crawl corpus, inspect it, and curate it."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from finepdfs.domain.crawl import CrawlQuery
from finepdfs.domain.dedup import PublishedDocumentView, exact_duplicate_keepers, plan_deduplication
from finepdfs.domain.document import Document
from finepdfs.infrastructure.cocoindex.runtime import Runtime, runtime_from_environment
from finepdfs.infrastructure.commoncrawl.source import CommonCrawlSource
from finepdfs.infrastructure.corpus.fetch import fetch_corpus
from finepdfs.infrastructure.corpus.index import CorpusIndex
from finepdfs.infrastructure.corpus.manifest import MANIFEST_NAME, CorpusManifest
from finepdfs.infrastructure.publishing.disk import PublishedCorpus

DEFAULT_CORPUS = Path("corpus")
DEFAULT_OUT = Path("out")

# Flag name to environment variable. "engine_command" is deliberately not called "command": a
# subparser argument with that name would overwrite the name of the chosen subcommand.
ENVIRONMENT_FOR_ENGINE = {
    "corpus": "FINEPDFS_CORPUS",
    "out": "FINEPDFS_OUT",
    "engine": "FINEPDFS_ENGINE",
    "base_url": "FINEPDFS_ENGINE_BASE_URL",
    "model": "FINEPDFS_ENGINE_MODEL",
    "api_key": "FINEPDFS_ENGINE_API_KEY",
    "engine_command": "FINEPDFS_ENGINE_COMMAND",
    "dpi": "FINEPDFS_ENGINE_DPI",
    "similarity_threshold": "FINEPDFS_SIMILARITY_THRESHOLD",
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

    inspect = commands.add_parser("inspect", help="summarise a corpus directory")
    inspect.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)

    dry_run = commands.add_parser("dry-run", help="curate the corpus in process and print what would happen")
    _add_engine_arguments(dry_run, out=False)

    run = commands.add_parser("run", help="run the incremental CocoIndex pipeline")
    _add_engine_arguments(run, out=True)

    return parser


def _add_engine_arguments(parser: argparse.ArgumentParser, *, out: bool) -> None:
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    if out:
        parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--engine",
        default="pymupdf",
        choices=["pymupdf", "docling", "paddleocr-vl", "mineru"],
        help="the engine that reads documents whose text layer is unusable",
    )
    parser.add_argument("--base-url", default="", help="OpenAI-compatible model server, for paddleocr-vl and mineru")
    parser.add_argument("--model", default="", help="model name to ask the server for")
    parser.add_argument("--api-key", default="", help="bearer token for the model server")
    parser.add_argument(
        "--engine-command",
        dest="engine_command",
        default="",
        help="local command, for docling and mineru",
    )
    parser.add_argument("--dpi", type=int, default=200, help="resolution used to render pages for a vision model")
    parser.add_argument(
        "--similarity-threshold",
        type=float,
        default=0.8,
        help="fingerprint similarity above which one document is a near-duplicate of another",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    if args.command == "fetch-corpus":
        return _fetch_corpus(args)
    if args.command == "inspect":
        return _inspect(args)
    if args.command == "dry-run":
        return _dry_run(args)
    if args.command == "run":
        return _run(args)
    raise AssertionError(f"unhandled command {args.command}")


def _fetch_corpus(args: argparse.Namespace) -> int:
    source = CommonCrawlSource()
    crawl_id = args.crawl or source.latest_crawl_id()
    query = CrawlQuery(crawl_id=crawl_id, domain=args.domain, limit=args.limit)
    print(f"fetching up to {args.limit} PDFs for {args.domain} from {crawl_id} into {args.corpus}")

    report = fetch_corpus(source, query, args.corpus)

    print(report.summary())
    for failure in report.failures[:5]:
        print(f"  failed: {failure.url} ({failure.reason})")
    return 1 if report.fetched == 0 and report.failed else 0


def _inspect(args: argparse.Namespace) -> int:
    manifest_path = args.corpus / MANIFEST_NAME
    manifest = CorpusManifest.load(manifest_path)
    if not manifest.entries:
        print(f"no corpus at {args.corpus} (looked for {manifest_path})", file=sys.stderr)
        return 2

    total_bytes = sum(entry.bytes for entry in manifest.entries)
    print(f"corpus: {args.corpus}")
    print(f"  crawl: {manifest.crawl_id}  domain: {manifest.domain}")
    print(f"  documents: {len(manifest.entries)}  distinct content: {len(manifest.digests)}")
    print(f"  bytes: {total_bytes}  mean: {total_bytes // max(len(manifest.entries), 1)}")
    for entry in manifest.entries[:5]:
        print(f"  {entry.digest[:12]}  {entry.bytes:>8}  {entry.url}")
    if len(manifest.entries) > 5:
        print(f"  ... and {len(manifest.entries) - 5} more")
    return 0


def _runtime_from_args(args: argparse.Namespace) -> Runtime:
    """The flags a run was given, expressed as the environment the flow reads."""
    for attribute, variable in ENVIRONMENT_FOR_ENGINE.items():
        value = getattr(args, attribute, None)
        if value is not None:
            os.environ[variable] = str(value)
    return runtime_from_environment()


def _dry_run(args: argparse.Namespace) -> int:
    runtime = _runtime_from_args(args)
    index = CorpusIndex(args.corpus)
    documents = index.documents()
    if not documents:
        print(f"no corpus at {args.corpus}", file=sys.stderr)
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
        similarity_threshold=args.similarity_threshold,
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

    _runtime_from_args(args)
    with coco.runtime():
        build_app().update_blocking(report_to_stdout=True)

    published = PublishedCorpus(args.out)
    records = published.read_records()
    exclusions = published.read_exclusions()
    print(f"recorded {len(records)} published documents and {len(exclusions)} exclusions under {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
