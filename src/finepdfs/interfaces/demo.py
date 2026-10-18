"""A repeatable demo run with its own corpus, output and warm CocoIndex database.

The demo's state lives under one root (`.demo/` in the repository by default, and ignored by git).
Nothing here wipes it: `restore` fetches the pinned Corpus once, `run` reuses the same database, and
only the explicit `reset` subcommand deletes the state. That is what makes an unchanged rerun free and
a policy-change rerun cheap -- the CocoIndex memo survives between commands.

    PYTHONPATH=src python -m finepdfs.interfaces.demo restore
    PYTHONPATH=src python -m finepdfs.interfaces.demo run --engine docling --engine-command docling

The pipeline itself still does the writing: this module only chooses the directories, exports the
environment the flow reads, and prints what the flow declared.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from finepdfs.infrastructure.corpus.manifest import CorpusManifest
from finepdfs.infrastructure.engines.registry import KNOWN_ENGINES
from finepdfs.infrastructure.publishing.disk import PublishedCorpus
from finepdfs.interfaces.restore import restore_pinned_corpus
from finepdfs.interfaces.run_log import RunClock, read_observed_run
from finepdfs.interfaces.summary import conclude_run, format_run_summary

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROOT = PROJECT_ROOT / ".demo"
DEFAULT_MANIFEST = PROJECT_ROOT / "docs" / "demo" / "pdf-demo-corpus-manifest.json"

# The environment the flow reads. Kept in one place so `demo_environment` and the runbook cannot
# disagree about which variable carries which directory.
CORPUS_VARIABLE = "FINEPDFS_CORPUS"
OUT_VARIABLE = "FINEPDFS_OUT"
DB_VARIABLE = "FINEPDFS_DB"


@dataclass(frozen=True, slots=True)
class DemoPaths:
    """Every directory one demo root owns."""

    root: Path
    corpus: Path
    out: Path
    database: Path

    @property
    def summary(self) -> Path:
        return PublishedCorpus(self.out).summary_path()


def demo_paths(root: Path | str | None = None) -> DemoPaths:
    """The demo's state layout. The default is `.demo/`, which is gitignored and persistent."""
    root = Path(root) if root is not None else DEFAULT_ROOT
    return DemoPaths(root=root, corpus=root / "corpus", out=root / "out", database=root / "state.db")


def demo_environment(
    paths: DemoPaths,
    *,
    engine: str = "pymupdf",
    engine_command: str = "",
    min_characters: int | None = None,
    similarity_threshold: float | None = None,
    search: bool = False,
) -> dict[str, str]:
    """The FINEPDFS_* values that point one run at the demo root and its warm database."""
    values = {
        CORPUS_VARIABLE: str(paths.corpus),
        OUT_VARIABLE: str(paths.out),
        DB_VARIABLE: str(paths.database),
        "FINEPDFS_ENGINE": engine,
    }
    if engine_command:
        values["FINEPDFS_ENGINE_COMMAND"] = engine_command
    if min_characters is not None:
        values["FINEPDFS_MIN_CHARACTERS"] = str(min_characters)
    if similarity_threshold is not None:
        values["FINEPDFS_SIMILARITY_THRESHOLD"] = str(similarity_threshold)
    # The demo's search stage needs the chunk export; a plain curation run does not declare it.
    values["FINEPDFS_SEARCH"] = "1" if search else "0"
    return values


def reset_demo(paths: DemoPaths) -> list[Path]:
    """Delete only the state this demo owns, and return what was removed.

    Deliberately explicit and deliberately partial: unrelated notes under the same root survive, so a
    reset cannot be mistaken for a cleanup of the user's files.
    """
    removed: list[Path] = []
    for path in (paths.corpus, paths.out, paths.database):
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.exists():
            path.unlink()
            removed.append(path)
    with suppress(OSError):
        paths.root.rmdir()  # only if what is left is empty; it never recurses
    return removed


def restore_demo_corpus(
    paths: DemoPaths,
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    source=None,
) -> int:
    """Replay the pinned manifest into the demo corpus through the same function restore-corpus uses."""
    return restore_pinned_corpus(manifest_path, paths.corpus, source=source)


@contextmanager
def _applied(values: Mapping[str, str]) -> Iterator[None]:
    """Set the flow's environment for one run and put the process back afterwards."""
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_demo(
    paths: DemoPaths,
    values: Mapping[str, str] | None = None,
    *,
    runtime=None,
) -> int:
    """Run the pipeline against the demo root. The flow, not this function, writes the outputs."""
    manifest_path = paths.corpus / "manifest.json"
    if not manifest_path.exists():
        print(f"no demo corpus at {paths.corpus}", file=sys.stderr)
        print("  restore the pinned Corpus first: python -m finepdfs.interfaces.demo restore", file=sys.stderr)
        return 2

    import cocoindex as coco

    from finepdfs.infrastructure.cocoindex import flow

    values = dict(values) if values is not None else demo_environment(paths)
    if runtime is not None:
        # A caller that built the run itself (a test, or a replay with a fixed engine) keeps it.
        flow.configure_runtime(runtime)
    # The demo measures the run it started; the flow declares only the reproducible counters.
    clock = RunClock()
    with _applied(values), coco.runtime():
        flow.build_app().update_blocking(report_to_stdout=True)
    summary_line, observed_log = conclude_run(paths.out, clock)

    published = PublishedCorpus(paths.out)
    if summary_line is None:
        print(f"the run declared no summary under {paths.out}", file=sys.stderr)
        return 1
    print(summary_line)
    print(f"observed timing: {observed_log} (not declared target state)")
    records = published.read_records()
    exclusions = published.read_exclusions()
    return 1 if exclusions and not records else 0


def demo_status(paths: DemoPaths) -> int:
    """Print the pinned Corpus size and the last declared run summary, without running anything."""
    manifest = CorpusManifest.load(paths.corpus / "manifest.json")
    if not manifest.entries:
        print(f"no demo corpus at {paths.corpus}", file=sys.stderr)
        return 2
    total_bytes = sum(entry.bytes for entry in manifest.entries)
    print(f"demo root: {paths.root}")
    print(
        f"  corpus: {len(manifest.entries)} crawl records / {len(manifest.digests)} documents"
        f" / {total_bytes} bytes ({manifest.crawl_id})"
    )
    summary = PublishedCorpus(paths.out).read_summary()
    if summary is None:
        print("  no run summary yet; run the demo to declare one", file=sys.stderr)
        return 0
    print(f"  {format_run_summary(summary, read_observed_run(paths.out))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finepdfs-demo",
        description="A repeatable PDF curation demo in an isolated, persistent state directory",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    restore = commands.add_parser("restore", help="restore the pinned Corpus into the demo root")
    restore.add_argument("--root", type=Path, default=None, help=f"demo state root (default: {DEFAULT_ROOT})")
    restore.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST, help="pinned manifest to replay"
    )

    run = commands.add_parser("run", help="run the pipeline against the demo root and print its summary")
    run.add_argument("--root", type=Path, default=None)
    run.add_argument("--engine", default="pymupdf", choices=list(KNOWN_ENGINES))
    run.add_argument("--engine-command", dest="engine_command", default="")
    run.add_argument("--min-characters", dest="min_characters", type=int, default=None)
    run.add_argument("--similarity-threshold", dest="similarity_threshold", type=float, default=None)
    run.add_argument(
        "--search",
        action="store_true",
        help="also declare searchable chunks, for the index-search and explore stages",
    )

    reset = commands.add_parser("reset", help="delete the demo corpus, output and database explicitly")
    reset.add_argument("--root", type=Path, default=None)

    status = commands.add_parser("status", help="show the demo Corpus size and the last run summary")
    status.add_argument("--root", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = demo_paths(args.root)

    if args.command == "restore":
        return restore_demo_corpus(paths, args.manifest)
    if args.command == "run":
        values = demo_environment(
            paths,
            engine=args.engine,
            engine_command=args.engine_command,
            min_characters=args.min_characters,
            similarity_threshold=args.similarity_threshold,
            search=args.search,
        )
        return run_demo(paths, values)
    if args.command == "reset":
        removed = reset_demo(paths)
        if not removed:
            print(f"nothing to reset under {paths.root}")
        for path in removed:
            print(f"removed {path}")
        return 0
    if args.command == "status":
        return demo_status(paths)
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
