"""One knob bounds engine calls in flight, across the whole run (ADR-0010 step 3).

The bound sits at the leaf, where the call actually happens -- one remote page request, or one local
command -- and never above it. A Document body that took a permit before waiting for its own pages would
deadlock the run with enough Documents, so these tests pin the no-deadlock property as well as the
ceiling: the run completes with more Documents than permits, for every engine that makes a call.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Self

import pymupdf
import pytest

from finepdfs.domain.document import Document
from finepdfs.infrastructure.cocoindex.runtime import runtime_from_environment
from finepdfs.infrastructure.engines.paddleocr_vl import PaddleOcrVlCliEngine
from finepdfs.infrastructure.engines.subprocess_support import CommandResult

REMOTE = {"FINEPDFS_ENGINE": "paddleocr-vl", "FINEPDFS_ENGINE_BASE_URL": "http://host:8000/v1", "FINEPDFS_ENGINE_MODEL": "m"}
LOCAL = {"FINEPDFS_ENGINE": "paddleocr-vl", "FINEPDFS_ENGINE_COMMAND": "paddleocr"}
DOCLING = {"FINEPDFS_ENGINE": "docling", "FINEPDFS_ENGINE_COMMAND": "docling"}
MINERU_CLI = {"FINEPDFS_ENGINE": "mineru", "FINEPDFS_ENGINE_COMMAND": "mineru"}
MINERU_HTTP = {"FINEPDFS_ENGINE": "mineru", "FINEPDFS_ENGINE_BASE_URL": "http://mineru.example:8080"}

# Every engine that makes an engine call, reached the way a run reaches it.
ENGINE_SETTINGS = {
    "paddleocr-vl over HTTP": REMOTE,
    "paddleocr-vl over the command line": LOCAL,
    "docling": DOCLING,
    "mineru over the command line": MINERU_CLI,
    "mineru over HTTP": MINERU_HTTP,
}


def _run_bound(engine) -> int:
    """The one bound the engine takes its permits from, wherever the adapter keeps it.

    A MinerU engine delegates to a backend, which is what makes the call, so the bound is the backend's.
    """
    backend = getattr(engine, "backend", None)
    return (backend or engine).calls.limit


@pytest.mark.parametrize("settings", ENGINE_SETTINGS.values(), ids=ENGINE_SETTINGS.keys())
def test_an_unset_concurrency_is_six_engine_calls_in_flight_for_every_engine(settings) -> None:
    """ADR-0010 step 3's default is a statement about the worker, and it is the same for every engine."""
    engine = runtime_from_environment(dict(settings)).engines[settings["FINEPDFS_ENGINE"]]

    assert _run_bound(engine) == 6


@pytest.mark.parametrize("settings", ENGINE_SETTINGS.values(), ids=ENGINE_SETTINGS.keys())
def test_a_configured_concurrency_reaches_every_engine_end_to_end(settings) -> None:
    """FINEPDFS_ENGINE_CONCURRENCY is the operator's bound: every engine a run builds has to carry it."""
    engine = runtime_from_environment(dict(settings, FINEPDFS_ENGINE_CONCURRENCY="3")).engines[
        settings["FINEPDFS_ENGINE"]
    ]

    assert _run_bound(engine) == 3


@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_a_concurrency_that_is_not_a_positive_number_fails_loudly_and_names_the_value(value: str) -> None:
    """A typo must not silently fall back to six calls in flight; the message points at the typo."""
    with pytest.raises(ValueError) as raised:
        runtime_from_environment(dict(LOCAL, FINEPDFS_ENGINE_CONCURRENCY=value))

    message = str(raised.value)
    assert value in message, "the rejected value is not in the message"
    assert "FINEPDFS_ENGINE_CONCURRENCY" in message, "the variable the value came from is not in the message"


def test_the_bound_refuses_zero_by_naming_the_setting_and_the_bound() -> None:
    """The engine-level check is the same one the environment parser makes, so it names the same thing."""
    from finepdfs.infrastructure.engines.concurrency import EngineCalls

    with pytest.raises(ValueError) as raised:
        EngineCalls(0)

    message = str(raised.value)
    assert "FINEPDFS_ENGINE_CONCURRENCY" in message, "the setting the number comes from is not in the message"
    assert "engine calls in flight" in message, "the bound the number sizes is not in the message"
    assert "0" in message, "the rejected value is not in the message"


def _one_page_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 120), "a synthetic scan")
    content = document.tobytes()
    document.close()
    return content


def _write_paddle_results(argv: list[str]) -> None:
    output_dir = Path(argv[argv.index("--save_path") + 1])
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "page-0001_res.json").write_text(
        json.dumps({"res": {"page_index": 0, "parsing_res_list": [{"block_content": "page 1"}]}}),
        encoding="utf-8",
    )


def _write_mineru_content_list(argv: list[str]) -> None:
    output_dir = Path(argv[argv.index("-o") + 1]) / "document"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "document_content_list.json").write_text(
        json.dumps([{"type": "text", "text": "page 1", "page_idx": 0}]), encoding="utf-8"
    )


def _write_docling_json(argv: list[str]) -> None:
    if "--output" not in argv:  # the one-off version probe, not a read
        return
    output_dir = Path(argv[argv.index("--output") + 1])
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "document.json").write_text(
        json.dumps({"body": {"children": [{"$ref": "#/texts/0"}]}, "texts": [{"label": "text", "text": "page 1", "prov": [{"page_no": 1}]}]}),
        encoding="utf-8",
    )


class CountingRunner:
    """Runs the command the way the tool would, and records how many commands ran at once.

    A second command, if the bound lets one in, meets the first at the rendezvous instead of merely
    missing it: with a bound of one it never comes, the rendezvous times out, and the run goes on
    serially -- so the ceiling is observed rather than hoped for.
    """

    def __init__(self, write: Callable[[list[str]], None] = _write_paddle_results) -> None:
        self.rendezvous = threading.Barrier(2, timeout=0.25)
        self.running = 0
        self.peak = 0
        self.commands: list[list[str]] = []
        self._write = write
        self._lock = threading.Lock()

    def run(self, argv, cwd=None) -> CommandResult:
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.commands.append(list(argv))
        try:
            # Alone, as it must be: the other commands are queued behind the bound, so the
            # rendezvous times out instead of being met.
            with contextlib.suppress(threading.BrokenBarrierError):
                self.rendezvous.wait()
            self._write(list(argv))
        finally:
            with self._lock:
                self.running -= 1
        return CommandResult(argv=tuple(argv), returncode=0, stdout="Docling version: 2.140.0\n", stderr="")


class Reply:
    """One MinerU answer: what ``urlopen`` returns, with the release of its in-flight request on exit."""

    def __init__(self, payload: bytes, release: Callable[[], None]) -> None:
        self._payload = payload
        self._release = release

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_) -> bool:
        self._release()
        return False

    def read(self) -> bytes:
        return self._payload


class CountingOpener:
    """Answers like a MinerU service, and records how many requests were in flight at once.

    The rendezvous is the same observation as ``CountingRunner``'s: a second request meets the first
    only if the bound lets it in, and under a bound of one it times out instead.
    """

    def __init__(self) -> None:
        self.rendezvous = threading.Barrier(2, timeout=0.25)
        self.running = 0
        self.peak = 0
        self._payload = json.dumps(
            {"results": {"document.pdf": {"content_list": [{"type": "text", "text": "page 1", "page_idx": 0}]}}}
        ).encode("utf-8")
        self._lock = threading.Lock()

    def __call__(self, request, timeout=None) -> Reply:
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        with contextlib.suppress(threading.BrokenBarrierError):
            self.rendezvous.wait()
        return Reply(self._payload, self._finished)

    def _finished(self) -> None:
        with self._lock:
            self.running -= 1


def _read_documents_on_their_own_threads(engine, names: list[str], content: bytes | None = None) -> list[Document]:
    """Start every Document's read at once, the way the flow does, and wait for all of them."""
    content = content if content is not None else _one_page_pdf()
    start = threading.Barrier(len(names) + 1)
    extracted: list = []
    failures: list[BaseException] = []

    def read(name: str) -> None:
        start.wait()
        try:
            extracted.append(engine.extract(Document(name=name, content=content)))
        except BaseException as error:  # a Document fails, the run does not
            failures.append(error)

    threads = [threading.Thread(target=read, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "the run stalled: a read never came back"
    assert failures == []
    return extracted


def test_the_cli_engine_never_runs_two_commands_at_once_across_documents(tmp_path) -> None:
    """A bound of one is one command for the run, not one per Document, and more Documents than
    permits still finish -- the bound is waited on, never held while a Document waits for its own pages.
    """
    runner = CountingRunner()
    engine = PaddleOcrVlCliEngine(work_dir=tmp_path, concurrency=1, runner=runner)

    extracted = _read_documents_on_their_own_threads(engine, [f"https://a.example/{number}.pdf" for number in range(3)])

    assert len(extracted) == 3, "every Document was read"
    assert runner.peak == 1, f"{runner.peak} doc_parser commands ran at once, and the knob said one"
    assert [page.text for extracted_document in extracted for page in extracted_document.pages] == [
        "page 1",
        "page 1",
        "page 1",
    ]


def test_the_docling_engine_never_runs_two_commands_at_once_across_documents(tmp_path) -> None:
    """Docling makes a local command per Document, so the knob has to bound it like any other.

    After ADR-0010's `def` body, every Document is read on a thread of its own; an unbounded Docling
    would launch one `docling convert` per Document at once. A bound of one means one command for the
    run, and three Documents still finish, so the bound is waited on rather than deadlocking the run.
    """
    from finepdfs.infrastructure.engines.docling_engine import DoclingEngine

    runner = CountingRunner(write=_write_docling_json)
    engine = DoclingEngine(command="docling", work_dir=tmp_path, concurrency=1, runner=runner)

    extracted = _read_documents_on_their_own_threads(engine, [f"https://a.example/{number}.pdf" for number in range(3)])

    assert len(extracted) == 3, "every Document was read"
    assert runner.peak == 1, f"{runner.peak} docling commands ran at once, and the knob said one"
    assert runner.running == 0, "a command was still running when every read had returned"
    assert [page.text for extracted_document in extracted for page in extracted_document.pages] == [
        "page 1",
        "page 1",
        "page 1",
    ]


def test_the_mineru_command_line_engine_never_runs_two_commands_at_once_across_documents(tmp_path) -> None:
    """MinerU's command line makes a local command per Document, bounded the same way Docling's is."""
    from finepdfs.infrastructure.engines.mineru import MineruEngine, MineruSubprocessBackend

    runner = CountingRunner(write=_write_mineru_content_list)
    engine = MineruEngine(
        backend=MineruSubprocessBackend(command="mineru", work_dir=tmp_path, concurrency=1, runner=runner)
    )

    extracted = _read_documents_on_their_own_threads(
        engine, [f"https://a.example/{number}.pdf" for number in range(3)], content=b"%PDF-1.7 fake"
    )

    assert len(extracted) == 3, "every Document was read"
    assert runner.peak == 1, f"{runner.peak} mineru commands ran at once, and the knob said one"
    assert [page.text for extracted_document in extracted for page in extracted_document.pages] == [
        "page 1",
        "page 1",
        "page 1",
    ]


def test_the_mineru_http_engine_never_sends_two_requests_at_once_across_documents() -> None:
    """MinerU's service path makes one remote request per Document, and the knob bounds that request."""
    from finepdfs.infrastructure.engines.mineru import MineruEngine, MineruHttpBackend

    opener = CountingOpener()
    engine = MineruEngine(
        backend=MineruHttpBackend(base_url="http://mineru.example:8080", concurrency=1, opener=opener)
    )

    extracted = _read_documents_on_their_own_threads(
        engine, [f"https://a.example/{number}.pdf" for number in range(3)], content=b"%PDF-1.7 fake"
    )

    assert len(extracted) == 3, "every Document was read"
    assert opener.peak == 1, f"{opener.peak} MinerU requests were in flight at once, and the knob said one"
    assert opener.running == 0, "a request was still in flight when every read had returned"
    assert [page.text for extracted_document in extracted for page in extracted_document.pages] == [
        "page 1",
        "page 1",
        "page 1",
    ]


def test_how_many_engine_calls_overlap_is_not_part_of_the_engine_identity() -> None:
    """The knob is infrastructure wiring, not something the Engine is: the memo key must not carry it.

    ADR-0005 keys an Extraction on the Engine and its deployment, so a run that raises the bound would
    re-read every Document already memoised if the number leaked into the detail.
    """
    for configuration in (REMOTE, LOCAL, MINERU_CLI, MINERU_HTTP):
        narrow = runtime_from_environment(dict(configuration, FINEPDFS_ENGINE_CONCURRENCY="1"))
        wide = runtime_from_environment(dict(configuration, FINEPDFS_ENGINE_CONCURRENCY="8"))
        name = configuration["FINEPDFS_ENGINE"]

        assert narrow.engines[name].identity == wide.engines[name].identity
