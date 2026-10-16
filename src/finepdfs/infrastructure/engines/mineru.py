"""MinerU as an OCR engine.

MinerU2.5-Pro has no text layer to fall back on: it reads page images and answers with a content list
of blocks (text, tables as HTML, equations as LaTeX). Both documented ways in are supported -- the
`mineru` command line and the /file_parse HTTP API -- and they share one parser, because the block
format is the same either way.
"""

from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText
from finepdfs.infrastructure.engines.subprocess_support import CommandRunner, SubprocessRunner, require_success

MINERU_MODEL = "MinerU2.5-Pro"


class MineruError(RuntimeError):
    """Raised when MinerU could not be run or produced nothing usable."""


class MineruBackend(Protocol):
    """How MinerU is reached: a local command or an HTTP service."""

    def describe(self) -> str: ...

    def content_list(self, document: Document) -> Sequence[dict]: ...


@dataclass(frozen=True, slots=True)
class MineruEngine:
    """Turns a content list into page texts."""

    backend: MineruBackend

    @property
    def identity(self) -> EngineIdentity:
        return EngineIdentity(name="mineru", detail=self.backend.describe())

    def extract(self, document: Document) -> ExtractedDocument:
        items = self.backend.content_list(document)
        return ExtractedDocument(pages=pages_from_content_list(items), engine=self.identity)


def pages_from_content_list(items: Sequence[dict]) -> tuple[PageText, ...]:
    """Group content list blocks by page, in order, one Page Text per page that has any."""
    by_page: dict[int, list[str]] = {}
    for item in items:
        piece = _block_text(item)
        if not piece:
            continue
        by_page.setdefault(int(item.get("page_idx", 0)), []).append(piece)
    return tuple(
        PageText(number=page_index + 1, text="\n\n".join(pieces))
        for page_index, pieces in sorted(by_page.items())
    )


def _block_text(item: dict) -> str:
    kind = item.get("type")
    if kind == "table":
        body = str(item.get("table_body") or item.get("text") or "").strip()
        return "\n\n".join(filter(None, (body, _captions(item))))
    if kind in {"image", "figure"}:
        return _captions(item)
    return str(item.get("text") or "").strip()


def _captions(item: dict) -> str:
    """Captions arrive as a list of strings; join them, and tolerate a plain string."""
    captions = item.get("img_caption") or item.get("table_caption") or []
    if isinstance(captions, str):
        captions = [captions]
    return " ".join(str(caption).strip() for caption in captions if str(caption).strip())


@dataclass(frozen=True, slots=True)
class MineruSubprocessBackend:
    """The local command line: `mineru -p <pdf> -o <outdir> -b pipeline`."""

    command: str = "mineru"
    work_dir: Path | None = None
    timeout_seconds: float = 1800.0
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    def describe(self) -> str:
        return f"{MINERU_MODEL}@cli:{self.command}"

    def content_list(self, document: Document) -> Sequence[dict]:
        with tempfile.TemporaryDirectory(dir=self.work_dir) as scratch:
            scratch_dir = Path(scratch)
            pdf_path = scratch_dir / "document.pdf"
            pdf_path.write_bytes(document.content)
            output_dir = scratch_dir / "out"
            require_success(
                self.runner.run([self.command, "-p", str(pdf_path), "-o", str(output_dir), "-b", "pipeline"])
            )
            candidates = sorted(output_dir.rglob("*content_list.json"))
            if not candidates:
                raise MineruError(f"mineru wrote no content list under {output_dir}")
            return json.loads(candidates[0].read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class MineruHttpBackend:
    """A MinerU service: multipart POST to /file_parse, asking for the content list."""

    base_url: str
    api_key: str = ""
    timeout_seconds: float = 1800.0
    opener: Any = None

    def describe(self) -> str:
        return f"{MINERU_MODEL}@{self.base_url}"

    def content_list(self, document: Document) -> Sequence[dict]:
        body, content_type = _multipart_body(document)
        headers = {"Content-Type": content_type}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.base_url.rstrip("/") + "/file_parse", data=body, headers=headers)
        opener = self.opener or urllib.request.urlopen
        try:
            with opener(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise MineruError(f"could not reach the MinerU service at {self.base_url}: {error}") from error
        return content_list_from_response(payload)


def content_list_from_response(payload: dict) -> list[dict]:
    """Find the content list in a MinerU answer, whichever key the version put it under."""
    results = payload.get("results")
    if isinstance(results, dict):
        for entry in results.values():
            if isinstance(entry, dict) and isinstance(entry.get("content_list"), list):
                return entry["content_list"]
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "page_idx" in value[0]:
            return value
    raise MineruError("the answer carried no content list")


def _multipart_body(document: Document) -> tuple[bytes, str]:
    boundary = "----finepdfs" + uuid.uuid4().hex
    fields = {
        "return_md": "true",
        "return_content_list": "true",
        "return_middle_json": "false",
        "response_format_zip": "false",
        "parse_method": "auto",
        "backend": "pipeline",
    }
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    filename = document.name or "document.pdf"
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n".encode()
        + document.content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
