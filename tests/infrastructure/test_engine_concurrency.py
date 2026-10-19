"""One knob bounds engine calls in flight, across the whole run (ADR-0010 step 3).

The bound sits at the leaf, where the call actually happens -- one remote page request, or one local
CLI invocation -- and never above it. A Document body that took a permit before waiting for its own
pages would deadlock the run with enough Documents, so these tests pin the no-deadlock property as well
as the ceiling: the run completes with more OCR Documents than permits.
"""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path

import pymupdf
import pytest

from finepdfs.domain.document import Document
from finepdfs.infrastructure.cocoindex.runtime import runtime_from_environment
from finepdfs.infrastructure.engines.paddleocr_vl import PaddleOcrVlCliEngine
from finepdfs.infrastructure.engines.subprocess_support import CommandResult

REMOTE = {"FINEPDFS_ENGINE": "paddleocr-vl", "FINEPDFS_ENGINE_BASE_URL": "http://host:8000/v1", "FINEPDFS_ENGINE_MODEL": "m"}
LOCAL = {"FINEPDFS_ENGINE": "paddleocr-vl", "FINEPDFS_ENGINE_COMMAND": "paddleocr"}


def test_an_unset_concurrency_is_six_engine_calls_in_flight_for_both_adapters() -> None:
    """ADR-0010 step 3's default is a statement about the worker, and it is the same for both shapes."""
    remote = runtime_from_environment(dict(REMOTE))
    local = runtime_from_environment(dict(LOCAL))

    assert remote.engines["paddleocr-vl"].concurrency == 6
    assert local.engines["paddleocr-vl"].concurrency == 6


def test_a_configured_concurrency_reaches_both_adapters_end_to_end() -> None:
    """FINEPDFS_ENGINE_CONCURRENCY is the operator's bound, so the engine a run builds has to carry it."""
    overridden = {"FINEPDFS_ENGINE_CONCURRENCY": "3"}

    remote = runtime_from_environment(dict(REMOTE, **overridden))
    local = runtime_from_environment(dict(LOCAL, **overridden))

    assert remote.engines["paddleocr-vl"].concurrency == 3
    assert local.engines["paddleocr-vl"].concurrency == 3


@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_a_concurrency_that_is_not_a_positive_number_fails_loudly_and_names_the_value(value: str) -> None:
    """A typo must not silently fall back to six calls in flight; the message points at the typo."""
    with pytest.raises(ValueError) as raised:
        runtime_from_environment(dict(LOCAL, FINEPDFS_ENGINE_CONCURRENCY=value))

    message = str(raised.value)
    assert value in message, "the rejected value is not in the message"
    assert "FINEPDFS_ENGINE_CONCURRENCY" in message, "the variable the value came from is not in the message"


def _one_page_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 120), "a synthetic scan")
    content = document.tobytes()
    document.close()
    return content


class CountingRunner:
    """Runs the command the way the tool would, and records how many invocations ran at once.

    A second invocation, if the bound lets one in, meets the first at the rendezvous instead of merely
    missing it: with a bound of one it never comes, the rendezvous times out, and the run goes on
    serially -- so the ceiling is observed rather than hoped for.
    """

    def __init__(self, page_count: int = 1) -> None:
        self.rendezvous = threading.Barrier(2, timeout=0.25)
        self.running = 0
        self.peak = 0
        self.invocations: list[list[str]] = []
        self._page_count = page_count
        self._lock = threading.Lock()

    def run(self, argv, cwd=None) -> CommandResult:
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.invocations.append(list(argv))
        try:
            # Alone, as it must be: the other invocations are queued behind the bound, so the
            # rendezvous times out instead of being met.
            with contextlib.suppress(threading.BrokenBarrierError):
                self.rendezvous.wait()
            output_dir = Path(argv[argv.index("--save_path") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            for index in range(1, self._page_count + 1):
                (output_dir / f"page-{index:04d}_res.json").write_text(
                    json.dumps({"res": {"page_index": index - 1, "parsing_res_list": [{"block_content": f"page {index}"}]}}),
                    encoding="utf-8",
                )
        finally:
            with self._lock:
                self.running -= 1
        return CommandResult(argv=tuple(argv), returncode=0, stdout="", stderr="")


def _read_documents_on_their_own_threads(engine, names: list[str]) -> list[Document]:
    """Start every Document's read at once, the way the flow does, and wait for all of them."""
    content = _one_page_pdf()
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


def test_how_many_engine_calls_overlap_is_not_part_of_the_engine_identity() -> None:
    """The knob is infrastructure wiring, not something the Engine is: the memo key must not carry it.

    ADR-0005 keys an Extraction on the Engine and its deployment, so a run that raises the bound would
    re-read every Document already memoised if the number leaked into the detail.
    """
    for configuration in (REMOTE, LOCAL):
        narrow = runtime_from_environment(dict(configuration, FINEPDFS_ENGINE_CONCURRENCY="1"))
        wide = runtime_from_environment(dict(configuration, FINEPDFS_ENGINE_CONCURRENCY="8"))

        assert narrow.engines["paddleocr-vl"].identity == wide.engines["paddleocr-vl"].identity
