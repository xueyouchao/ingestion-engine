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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText
from finepdfs.infrastructure.engines.subprocess_support import CommandRunner, SubprocessRunner, require_success

VERSION_PATTERN = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+.\w]*)?")
# A tool version is a property of the installed executable, so one probe per command is enough.
_VERSION_CACHE: dict[str, str] = {}


class DoclingError(RuntimeError):
    """Raised when Docling could not be run or wrote no readable DoclingDocument JSON."""


@dataclass(frozen=True, slots=True)
class DoclingEngine:
    """Runs `docling convert --to json --output <dir> <pdf>` and reads the page-aware JSON back."""

    command: str = "docling"
    work_dir: Path | None = None
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    @property
    def identity(self) -> EngineIdentity:
        """The installed tool's name and version, never the path it happens to live at.

        ADR-0005 makes the tool version an identity input: a Docling upgrade must re-read the corpus.
        The probe is best effort -- a tool that cannot report a version still gets a stable name --
        and its result is cached per command.
        """
        command_name = Path(self.command).name or "docling"
        version = _tool_version(self.runner, self.command)
        detail = f"cli:{command_name}" + (f":v{version}" if version else "")
        return EngineIdentity(name="docling", detail=detail)

    def extract(self, document: Document) -> ExtractedDocument:
        identity = self.identity
        with tempfile.TemporaryDirectory(dir=self.work_dir) as scratch:
            scratch_dir = Path(scratch)
            pdf_path = scratch_dir / "document.pdf"
            pdf_path.write_bytes(document.content)
            output_dir = scratch_dir / "out"
            require_success(
                self.runner.run(
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
            )
            json_files = sorted(output_dir.rglob("*.json"))
            if not json_files:
                raise DoclingError(f"docling wrote no DoclingDocument JSON under {output_dir}")
            try:
                payload = json.loads(json_files[0].read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise DoclingError(f"docling wrote invalid JSON in {json_files[0]}: {error}") from error
            pages = pages_from_docling_json(payload)
        return ExtractedDocument(pages=pages, engine=identity)


def _tool_version(runner: CommandRunner, command: str) -> str:
    """The version Docling reports, or an empty string when the probe cannot answer."""
    if command in _VERSION_CACHE:
        return _VERSION_CACHE[command]
    try:
        result = runner.run([command, "--version"])
    except Exception:  # A missing tool is the conversion's failure to report, not this best-effort probe's.
        return ""
    if result.returncode != 0:
        return ""
    first_line = (result.stdout or result.stderr).strip().splitlines()
    match = VERSION_PATTERN.search(first_line[0]) if first_line else None
    if match is None:
        return ""
    _VERSION_CACHE[command] = match.group(0)
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
