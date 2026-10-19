"""Docling as an OCR engine: the local command line, MIT licensed.

Docling's Markdown export carries no page boundaries on the command line, so the adapter asks for
the documented DoclingDocument JSON instead. Each text item records its original 1-based page in
``prov[].page_no``, and the body tree records reading order; both are read here so a multi-page
Document keeps its pages instead of collapsing into one Page Text.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText
from finepdfs.infrastructure.engines.concurrency import DEFAULT_ENGINE_CONCURRENCY, EngineCalls
from finepdfs.infrastructure.engines.subprocess_support import CommandRunner, SubprocessRunner, require_success

VERSION_PATTERN = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+.\w]*)?")


class DoclingError(RuntimeError):
    """Raised when Docling could not be run or wrote no readable DoclingDocument JSON."""


class DoclingVersionError(RuntimeError):
    """Raised when the Docling version probe could not name the installed tool's version.

    ADR-0005 puts the tool version in the Engine Identity, which is part of the memo key. A probe that
    could not answer must fail here rather than drop the version: an identity of ``cli:docling`` would
    let an upgraded Docling reuse an Extraction the previous version produced.
    """


@dataclass(frozen=True, slots=True)
class DoclingEngine:
    """Runs `docling convert --to json --output <dir> <pdf>` and reads the page-aware JSON back."""

    command: str = "docling"
    work_dir: Path | None = None
    # How many engine calls the run may keep in flight (ADR-0010 step 3). An InitVar, not a field: the
    # number is not kept here, it lives in the one EngineCalls every command takes its permit from, so no
    # second copy of it can disagree with the bound the command actually waits on.
    concurrency: InitVar[int] = DEFAULT_ENGINE_CONCURRENCY
    runner: CommandRunner = field(default_factory=SubprocessRunner)
    # The run's one bound, held around the command and nothing else: the temp directory, the JSON read
    # back and the parse all stay outside it, so a Document that waits for a permit holds nothing else.
    calls: EngineCalls = field(init=False, compare=False, repr=False)

    def __post_init__(self, concurrency: int) -> None:
        object.__setattr__(self, "calls", EngineCalls(concurrency))

    @property
    def identity(self) -> EngineIdentity:
        """The installed tool's name and version, never the path it happens to live at.

        ADR-0005 makes the tool version an identity input: a Docling upgrade must re-read the corpus.
        A probe that cannot report a version raises ``DoclingVersionError`` instead of quietly naming an
        identity without one, and its answer is cached per command and runner.
        """
        command_name = Path(self.command).name or "docling"
        version = _tool_version(self.runner, self.command, self.calls)
        return EngineIdentity(name="docling", detail=f"cli:{command_name}:v{version}")

    def extract(self, document: Document) -> ExtractedDocument:
        identity = self.identity
        with tempfile.TemporaryDirectory(dir=self.work_dir) as scratch:
            scratch_dir = Path(scratch)
            pdf_path = scratch_dir / "document.pdf"
            pdf_path.write_bytes(document.content)
            output_dir = scratch_dir / "out"
            with self.calls.permit():
                result = self.runner.run(
                    [
                        self.command,
                        "convert",
                        "--to",
                        "json",
                        "--image-export-mode",
                        "placeholder",
                        "--output",
                        str(output_dir),
                        str(pdf_path),
                    ]
                )
            require_success(result)
            json_files = sorted(output_dir.rglob("*.json"))
            if not json_files:
                raise DoclingError(f"docling wrote no DoclingDocument JSON under {output_dir}")
            try:
                payload = json.loads(json_files[0].read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise DoclingError(f"docling wrote invalid JSON in {json_files[0]}: {error}") from error
            pages = pages_from_docling_json(payload)
        return ExtractedDocument(pages=pages, engine=identity)


@lru_cache(maxsize=32)
def _tool_version(runner: CommandRunner, command: str, calls: EngineCalls) -> str:
    """The version Docling reports for this command, or a named error when it cannot report one.

    A tool version is a property of the installed executable, so the probe runs once per command and
    runner: the runner is part of the key because it is what answers the probe, and a cached answer
    from another runner would be a version this engine never asked about. The probe is a local command
    like any other, so it takes the run's bound while it runs: after ADR-0010's ``def`` body every
    Document is read on a thread of its own, and several first Documents probing at once would be
    exactly the unbounded commands the knob exists to prevent.
    """
    try:
        with calls.permit():
            result = runner.run([command, "--version"])
    except Exception as error:  # a missing tool or a runner failure is this probe's own failure
        raise DoclingVersionError(f"docling version probe failed for {command!r}: {error}") from error
    if result.returncode != 0:
        raise DoclingVersionError(
            f"docling version probe failed for {command!r}: it exited with {result.returncode}"
        )
    lines = (result.stdout or result.stderr).strip().splitlines()
    match = VERSION_PATTERN.search(lines[0]) if lines else None
    if match is None:
        raise DoclingVersionError(f"docling version probe for {command!r} reported no version")
    return match.group(0)


def pages_from_docling_json(payload: Mapping[str, Any]) -> tuple[PageText, ...]:
    """One Page Text per original 1-based page, in reading order, or no pages for an empty document.

    The body tree is the reading order Docling documents; the Markdown exporter reads the same body
    layer. Items without provenance cannot be placed on an original page and are skipped, and the
    furniture layer is not reached from the body, which matches the Markdown export's default.
    """
    blocks: list[tuple[int, str]] = []
    body = payload.get("body")
    if isinstance(body, Mapping):
        children = body.get("children")
        if isinstance(children, list):
            for child in children:
                _collect_blocks(payload, child, blocks)
    if not blocks:
        # A body tree is expected, but a JSON file whose body is empty still carries the text items.
        texts = payload.get("texts")
        if isinstance(texts, list):
            for item in texts:
                if not isinstance(item, Mapping):
                    continue
                page = _page_number(item)
                text = _item_text(item)
                if page is not None and text:
                    blocks.append((page, text))
    grouped: dict[int, list[str]] = {}
    for page, text in blocks:
        grouped.setdefault(page, []).append(text)
    return tuple(PageText(number=number, text="\n\n".join(grouped[number])) for number in sorted(grouped))


def _collect_blocks(payload: Mapping[str, Any], ref: object, blocks: list[tuple[int, str]]) -> None:
    item = _resolve(payload, ref)
    if item is None:
        return
    page = _page_number(item)
    text = _item_text(item)
    if page is not None and text:
        blocks.append((page, text))
    children = item.get("children")
    if isinstance(children, list):
        for child in children:
            _collect_blocks(payload, child, blocks)


def _resolve(payload: Mapping[str, Any], ref: object) -> Mapping[str, Any] | None:
    """Resolve a Docling ``{"$ref": "#/texts/4"}`` reference, ignoring anything malformed."""
    target = ref.get("$ref") if isinstance(ref, Mapping) else None
    if not isinstance(target, str) or not target.startswith("#/"):
        return None
    parts = target[2:].split("/")
    if len(parts) != 2:
        return None
    kind, index = parts
    items = payload.get(kind)
    if not isinstance(items, list):
        return None
    try:
        position = int(index)
    except ValueError:
        return None
    if not 0 <= position < len(items):
        return None
    item = items[position]
    return item if isinstance(item, Mapping) else None


def _page_number(item: Mapping[str, Any]) -> int | None:
    """The original 1-based page from the first provenance entry, or None when unplaceable."""
    provenance = item.get("prov")
    if not isinstance(provenance, list):
        return None
    for entry in provenance:
        if not isinstance(entry, Mapping):
            continue
        page = entry.get("page_no")
        if isinstance(page, int) and page >= 1:
            return page
    return None


def _item_text(item: Mapping[str, Any]) -> str:
    if item.get("label") == "table":
        return _table_text(item)
    text = item.get("text")
    return text.strip() if isinstance(text, str) and text.strip() else ""


def _table_text(table: Mapping[str, Any]) -> str:
    """Table cells as tabular lines; Docling keeps them outside the text items."""
    data = table.get("data")
    cells = data.get("table_cells") if isinstance(data, Mapping) else None
    if not isinstance(cells, list):
        return ""
    rows: dict[int, dict[int, str]] = {}
    for cell in cells:
        if not isinstance(cell, Mapping):
            continue
        text = cell.get("text")
        row = cell.get("start_row_offset_idx")
        column = cell.get("start_col_offset_idx")
        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(row, int) or not isinstance(column, int):
            continue
        rows.setdefault(row, {})[column] = text.strip()
    lines = [" | ".join(row[column] for column in sorted(row)) for _, row in sorted(rows.items())]
    return "\n".join(line for line in lines if line)
