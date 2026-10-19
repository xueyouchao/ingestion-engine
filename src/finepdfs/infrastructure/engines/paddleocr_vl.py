"""PaddleOCR-VL as an OCR engine.

Two documented ways in, both supported here: a vLLM/OpenAI-compatible server (one page image per
request, prompt "OCR:"), and the local command line, which writes one JSON result per page. PaddleOCR
and its weights are Apache-2.0.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import InitVar, dataclass, field
from pathlib import Path

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText
from finepdfs.infrastructure.engines.concurrency import DEFAULT_ENGINE_CONCURRENCY, EngineCalls
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

    def __init__(
        self,
        transport: VlmChatTransport,
        dpi: int = 200,
        detail: str = "",
        concurrency: int = DEFAULT_ENGINE_CONCURRENCY,
    ) -> None:
        self._transport = transport
        self._dpi = dpi
        # The run's one bound, not this read's: the Engine instance is shared by every Document the run
        # reads, so the pool it may fill lives on the instance and outlives any one Document. The number
        # lives here and nowhere else (``EngineCalls.limit``), so the window below and the pool cannot
        # be sized by two copies of it that disagree.
        self.calls = EngineCalls(concurrency)
        self._identity = EngineIdentity(
            name="paddleocr-vl", detail=paddleocr_detail(detail or transport.endpoint.describe(), dpi)
        )

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document) -> ExtractedDocument:
        """Read every page, overlapping the requests while rendering them one at a time.

        Rendering is serial and stays on this thread: PyMuPDF is not thread-safe, and every page goes
        through the one open PDF handle, so moving a render into the pool would risk a crash that takes
        the whole run with it rather than failing one Document (ADR-0010 step 2). Only the blocking page
        request is handed to the pool, so page *k*'s request is in flight while page *k+1* renders.
        Pages are collected oldest first, never in completion order, so their numbers are their own. The
        pool the requests go to belongs to the Engine rather than to this call, so Documents waiting on
        each other share one bound instead of each bringing its own (ADR-0010 step 3).

        A page that fails fails this read, and every page request the read has queued but not started is
        cancelled on the way out, so the run's bound is not spent on a Document that has already failed.
        A request a worker has already started cannot be cancelled: it runs to completion and is not
        recalled.
        """
        pages: dict[int, PageText] = {}
        in_flight: deque[tuple[int, Future[str]]] = deque()

        def collect(pending: tuple[int, Future[str]]) -> None:
            """Take the oldest request's answer, so pages leave the pool in page-number order."""
            page_number, future = pending
            answer = future.result()
            pages[page_number] = PageText(number=page_number, text=strip_model_preamble(answer, OCR_PROMPT))

        with PdfDocument(document.content) as pdf:
            try:
                for page_number in range(1, pdf.page_count + 1):
                    while len(in_flight) >= self.calls.limit:  # bound the images held for in-flight requests
                        collect(in_flight.popleft())
                    image = pdf.render_page_png(page_number, dpi=self._dpi)
                    in_flight.append(
                        (page_number, self.calls.submit(self._transport.complete_with_page_image, OCR_PROMPT, image))
                    )
                while in_flight:
                    collect(in_flight.popleft())
            except BaseException:
                # A failed page must not keep paying for the pages waiting behind it, nor hold the run's
                # bound while they wait: give them up on the way out. No waiting -- a request a worker has
                # already started cannot be cancelled and is left to finish on its own.
                for _, queued in in_flight:
                    queued.cancel()
                raise
        return ExtractedDocument(pages=tuple(pages[number] for number in sorted(pages)), engine=self._identity)


@dataclass(frozen=True, slots=True)
class PaddleOcrVlCliEngine:
    """Runs the local `paddleocr doc_parser` command, one rendered page at a time.

    The command writes <stem>_res.json next to its other outputs; a PDF would overwrite page results
    with the same stem, so pages are rendered to images first and each page keeps its own result.
    """

    command: str = "paddleocr"
    work_dir: Path | None = None
    dpi: int = 200
    # How many engine calls the run may keep in flight (ADR-0010 step 3). An InitVar, not a field: the
    # number is not kept here, it lives in the one EngineCalls the read takes its permit from, so no
    # second copy of it can disagree with the bound the command actually waits on.
    concurrency: InitVar[int] = DEFAULT_ENGINE_CONCURRENCY
    runner: CommandRunner = field(default_factory=SubprocessRunner)
    # The run's one bound, held around the command and nothing else: rendering stays outside it, so a
    # Document that waits for a slot is not holding one while it works.
    calls: EngineCalls = field(init=False, compare=False, repr=False)

    def __post_init__(self, concurrency: int) -> None:
        object.__setattr__(self, "calls", EngineCalls(concurrency))

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

            with self.calls.permit():
                result = self.runner.run(
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
            require_success(result)

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
