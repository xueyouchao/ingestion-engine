"""Where the local search index and the output it mirrors live, resolved in one place.

`index-search`, `search`, `explore` and `ask` all read or write the same derived index beside the same
declared chunks. Before this helper each resolved the path its own way, so `ask` silently ignored
`FINEPDFS_SEARCH_INDEX` and `index-search` preferred `--out` over it. One resolver, used by all four,
is the only way the flag, the documented variable and the default cannot drift apart (ADR-0008).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_OUT = Path("out")
SEARCH_INDEX_NAME = "search.db"
SEARCH_INDEX_VARIABLE = "FINEPDFS_SEARCH_INDEX"
OUT_VARIABLE = "FINEPDFS_OUT"


def resolve_out_dir(out: Path | str | None = None, *, environ: Mapping[str, str] | None = None) -> Path:
    """The published output directory: the flag, else ``FINEPDFS_OUT``, else ``out/``."""
    if out is not None:
        return Path(out)
    return Path(_values(environ).get(OUT_VARIABLE, DEFAULT_OUT))


def resolve_search_index(
    *,
    out: Path | str | None = None,
    index: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """The index path: an explicit ``--index``, else ``FINEPDFS_SEARCH_INDEX``, else ``<out>/search.db``."""
    if index is not None:
        return Path(index)
    configured = _values(environ).get(SEARCH_INDEX_VARIABLE, "")
    if configured:
        return Path(configured)
    return resolve_out_dir(out, environ=environ) / SEARCH_INDEX_NAME


def _values(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return environ if environ is not None else os.environ
