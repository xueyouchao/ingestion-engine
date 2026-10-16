"""Docling as an OCR engine: the local command line, MIT licensed.

Docling writes a document-level Markdown file. If the Markdown carries page markers they are honoured;
otherwise the whole document is one Page Text, which is honest about what the tool returned.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText
from finepdfs.infrastructure.engines.subprocess_support import CommandRunner, SubprocessRunner, require_success

FORM_FEED = "\f"
PAGE_MARKER = re.compile(r"^(?:<!--\s*page[:\s]+(\d+)\s*-->|" + FORM_FEED + r")\s*$", re.IGNORECASE | re.MULTILINE)


class DoclingError(RuntimeError):
    """Raised when Docling could not be run or wrote no Markdown."""


@dataclass(frozen=True, slots=True)
class DoclingEngine:
    """Runs `docling --to md --output <dir> <pdf>` and reads the Markdown back."""

    command: str = "docling"
    work_dir: Path | None = None
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    @property
    def identity(self) -> EngineIdentity:
        return EngineIdentity(name="docling", detail=f"cli:{self.command}")

    def extract(self, document: Document) -> ExtractedDocument:
        with tempfile.TemporaryDirectory(dir=self.work_dir) as scratch:
            scratch_dir = Path(scratch)
            pdf_path = scratch_dir / "document.pdf"
            pdf_path.write_bytes(document.content)
            output_dir = scratch_dir / "out"
            require_success(
                self.runner.run([self.command, "--to", "md", "--output", str(output_dir), str(pdf_path)])
            )
            markdown_files = sorted(output_dir.rglob("*.md"))
            if not markdown_files:
                raise DoclingError(f"docling wrote no Markdown under {output_dir}")
            pages = pages_from_markdown(markdown_files[0].read_text(encoding="utf-8"))
        return ExtractedDocument(pages=pages, engine=self.identity)


def pages_from_markdown(markdown: str) -> tuple[PageText, ...]:
    """One Page Text per page marker, or a single Page Text when the tool gives no page structure."""
    markers = list(PAGE_MARKER.finditer(markdown))
    if markers:
        pages: list[PageText] = []
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(markdown)
            text = markdown[marker.end() : end].strip()
            if text:
                pages.append(PageText(number=int(marker.group(1)) if marker.group(1) else index + 1, text=text))
        if pages:
            return tuple(pages)
    text = markdown.replace(FORM_FEED, "\n").strip()
    return (PageText(number=1, text=text),) if text else ()
