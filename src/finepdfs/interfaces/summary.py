"""Render a run summary as one terminal line.

The browser reads `out/run-summary.json`; this is only the terminal view of the same declared file,
so nothing here recomputes a counter or writes anything. Elapsed time is not part of that file: it is
observed by the command wrapper around the run and printed only when it was recorded, labelled as an
observation rather than as declared state.
"""

from __future__ import annotations

from finepdfs.interfaces.run_log import observed_elapsed


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
