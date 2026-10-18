"""Render a run summary as one terminal line.

The browser reads `out/run-summary.json`; this is only the terminal view of the same declared file,
so nothing here recomputes a counter or writes anything. Elapsed time is not part of that file: it is
observed by the command wrapper around the run and printed only when it was recorded, labelled as an
observation rather than as declared state.
"""

from __future__ import annotations

from pathlib import Path

from finepdfs.infrastructure.publishing.disk import PublishedCorpus
from finepdfs.interfaces.run_log import RunClock, observed_elapsed, read_observed_run, write_observed_run


def conclude_run(out_dir: Path | str, clock: RunClock) -> tuple[str | None, Path]:
    """Stop the clock, record the observation, and render the summary the flow declared.

    The order -- observation beside the outputs, then the declared summary read back -- is the same for
    every command that wraps a run, so `run` and the demo consume this one function instead of each
    repeating it. The line is ``None`` when the flow declared no summary; the path is where the
    observation went, so the caller can label it as observed rather than declared.
    """
    log_path = write_observed_run(out_dir, clock.stop())
    summary = PublishedCorpus(Path(out_dir)).read_summary()
    if summary is None:
        return None, log_path
    return format_run_summary(summary, read_observed_run(out_dir)), log_path


def format_run_summary(summary: dict, observed: dict | None = None) -> str:
    """A compact, copy-pasteable line: what was read, reused, published and left out."""
    corpus = summary["corpus"]
    routing = summary["routing"]
    extraction = summary["extraction"]
    published = summary["published"]
    exclusions = summary["exclusions"]

    kinds = ", ".join(
        f"{kind}={count}" for kind, count in exclusions["by_kind"].items() if count
    ) or "none"
    line = (
        f"run summary: {corpus['crawl_records']} crawl records / {corpus['documents']} documents"
        f" | routing native={routing['native']} ocr={routing['ocr']}"
        f" | extraction calls={extraction['calls']} reused={extraction['reused']}"
        f" | published={published['documents']} documents, {published['page_records']} page records"
        f" | exclusions={exclusions['total']} ({kinds})"
    )
    elapsed = observed_elapsed(observed)
    if elapsed is not None:
        line += f" | elapsed {elapsed:.1f}s (observed)"
    return line
