"""PaddleOCR-VL as an OCR engine.

Two documented ways in, both supported here: a vLLM/OpenAI-compatible server (one page image per
request, prompt "OCR:"), and the local command line, which writes one JSON result per page. PaddleOCR
and its weights are Apache-2.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText
from finepdfs.infrastructure.engines.pdf_document import PdfDocument
from finepdfs.infrastructure.engines.subprocess_support import CommandRunner, SubprocessRunner, require_success
from finepdfs.infrastructure.engines.vlm_chat import VlmChatTransport, strip_model_preamble

OCR_PROMPT = "OCR:"
TABLE_PROMPT = "Table Recognition:"
PIPELINE_VERSION = "v1.6"


def paddleocr_detail(base: str, dpi: int) -> str:
    """The deployment detail a PaddleOCR-VL engine remembers, with the page DPI it rendered at.

    The model reads page images rendered at this DPI, so the same bytes at 300 DPI can produce text the
    200 DPI read did not, and ADR-0005 and the spec list page DPI among the engine identity's inputs.
    Without it in the detail, changing ``FINEPDFS_ENGINE_DPI`` would be served the other read. The base
    is the model and address the adapter names itself, or the deployment name a provider pinned: that
    name replaces the adapter's default, but it still has to carry the DPI with it.
    """
    return f"{base}@dpi{dpi}"


class PaddleOcrError(RuntimeError):
    """Raised when PaddleOCR-VL could not be read or did not produce a result."""


class PaddleOcrVlEngine:
    """Reads every page through a PaddleOCR-VL server that speaks the OpenAI chat protocol."""

    def __init__(self, transport: VlmChatTransport, dpi: int = 200, detail: str = "") -> None:
        self._transport = transport
        self._dpi = dpi
        self._identity = EngineIdentity(
            name="paddleocr-vl", detail=paddleocr_detail(detail or transport.endpoint.describe(), dpi)
        )

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document) -> ExtractedDocument:
        pages: list[PageText] = []
        with PdfDocument(document.content) as pdf:
            for page_number in range(1, pdf.page_count + 1):
                image = pdf.render_page_png(page_number, dpi=self._dpi)
                answer = self._transport.complete_with_page_image(OCR_PROMPT, image)
                pages.append(PageText(number=page_number, text=strip_model_preamble(answer, OCR_PROMPT)))
        return ExtractedDocument(pages=tuple(pages), engine=self._identity)


@dataclass(frozen=True, slots=True)
class PaddleOcrVlCliEngine:
    """Runs the local `paddleocr doc_parser` command, one rendered page at a time.

    The command writes <stem>_res.json next to its other outputs; a PDF would overwrite page results
    with the same stem, so pages are rendered to images first and each page keeps its own result.
    """

    command: str = "paddleocr"
    work_dir: Path | None = None
    dpi: int = 200
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    @property
    def identity(self) -> EngineIdentity:
        return EngineIdentity(
            name="paddleocr-vl", detail=paddleocr_detail(f"cli:{self.command}:{PIPELINE_VERSION}", self.dpi)
        )

    def extract(self, document: Document) -> ExtractedDocument:
        import tempfile

        with tempfile.TemporaryDirectory(dir=self.work_dir) as scratch:
            scratch_dir = Path(scratch)
            images_dir = scratch_dir / "pages"
            output_dir = scratch_dir / "results"
            images_dir.mkdir()

            with PdfDocument(document.content) as pdf:
                page_count = pdf.page_count
                for page_number in range(1, page_count + 1):
                    rendered = pdf.render_page_png(page_number, dpi=self.dpi)
                    (images_dir / f"page-{page_number:04d}.png").write_bytes(rendered)

            require_success(
                self.runner.run(
                    [
                        self.command,
                        "doc_parser",
                        "-i",
                        str(images_dir),
                        "--pipeline_version",
                        PIPELINE_VERSION,
                        "--save_path",
                        str(output_dir),
                    ]
                )
            )

            pages = tuple(pages_from_paddle_output(output_dir, page_count))
        return ExtractedDocument(pages=pages, engine=self.identity)


def pages_from_paddle_output(output_dir: Path, page_count: int) -> list[PageText]:
    """Collect one page per rendered page from a PaddleOCR output directory."""
    import json

    results = sorted(output_dir.glob("*_res.json"))
    if not results:
        raise PaddleOcrError(f"paddleocr wrote no *_res.json under {output_dir}")
    pages = []
    for index, result_path in enumerate(results[:page_count], start=1):
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        page = page_from_paddle_result(payload, fallback_number=index)
        pages.append(PageText(number=page.number, text=page.text))
    return pages


def page_from_paddle_result(payload: dict, fallback_number: int = 1) -> PageText:
    """Read one PaddleOCR result document: its blocks in reading order become the page text.

    The result shape is the documented one: {"res": {"page_index": 0,
    "parsing_res_list": [{"block_content": ..., "block_label": ..., "block_order": ...}]}}.
    """
    result = payload.get("res", payload)
    blocks = result.get("parsing_res_list")
    if not isinstance(blocks, list):
        raise PaddleOcrError("the result carries no parsing_res_list; was this written by paddleocr doc_parser?")

    ordered = sorted(blocks, key=lambda block: (block.get("block_order") is None, block.get("block_order") or 0))
    text = "\n\n".join(str(block.get("block_content", "")).strip() for block in ordered if block.get("block_content"))
    page_index = result.get("page_index")
    number = int(page_index) + 1 if isinstance(page_index, int) else fallback_number
    return PageText(number=number, text=text)
