"""The demo command: isolated, persistent state, and an explicit reset.

These tests drive the command's own functions with a fake engine. They prove the state layout and
that a run does not silently wipe the warm cache; they prove nothing about real OCR.
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from finepdfs.application.curate import CurationPolicy, CurationStep, ExtractionStep
from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument, PageText, TextLayerQuality
from finepdfs.infrastructure.cocoindex.runtime import Runtime
from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path
from finepdfs.infrastructure.language.script import ScriptLanguageIdentifier
from finepdfs.interfaces import demo

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
URL = "https://a.example/one.pdf"


class CountingEngine:
    """A stand-in engine; the demo tests only care that the flow wrote where it was told."""

    def __init__(self) -> None:
        self._identity = EngineIdentity(name="pymupdf", detail="demo-test")
        self.calls = 0

    @property
    def identity(self) -> EngineIdentity:
        return self._identity

    def extract(self, document: Document, sink=None) -> ExtractedDocument:
        # ``sink`` is part of the engine's interface (ADR-0014); this stand-in has nothing to report.
        self.calls += 1
        return ExtractedDocument(
            pages=(PageText(number=1, text="demo word " * 200),), engine=self._identity
        )


class NativeProbe:
    def measure(self, content: bytes) -> TextLayerQuality:
        return TextLayerQuality(
            page_count=1, pages_with_text=1, character_count=2400, garbled_ratio=0.0, image_dominated_pages=0
        )


def _write_corpus(corpus_dir: Path) -> None:
    body = (FIXTURES / "warc_payload.bin").read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    (corpus_dir / "pdfs").mkdir(parents=True)
    (corpus_dir / relative_pdf_path(digest)).write_bytes(body)
    CorpusManifest(
        crawl_id="CC-MAIN-2026-39",
        domain="a.example",
        entries=[
            CorpusEntry(
                url=URL,
                digest=digest,
                file=relative_pdf_path(digest),
                bytes=len(body),
                crawl_id="CC-MAIN-2026-39",
                warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
                offset=1,
                length=len(body),
            )
        ],
    ).save(corpus_dir / "manifest.json")


def _runtime(paths: demo.DemoPaths, engine: CountingEngine) -> Runtime:
    policy = CurationPolicy(engine_name="pymupdf")
    return Runtime(
        corpus_dir=paths.corpus,
        out_dir=paths.out,
        policy=policy,
        extraction=ExtractionStep(NativeProbe(), {"pymupdf": engine}, policy),
        curation=CurationStep(ScriptLanguageIdentifier(), policy),
    )


def test_the_demo_uses_one_isolated_root_for_corpus_output_and_database(tmp_path) -> None:
    paths = demo.demo_paths(tmp_path / "demo")

    assert paths.corpus == tmp_path / "demo" / "corpus"
    assert paths.out == tmp_path / "demo" / "out"
    assert paths.database == tmp_path / "demo" / "state.db"
    assert paths.summary == tmp_path / "demo" / "out" / "run-summary.json"

    values = demo.demo_environment(paths, engine="docling", engine_command="/opt/docling", min_characters=500)
    assert values["FINEPDFS_CORPUS"] == str(paths.corpus)
    assert values["FINEPDFS_OUT"] == str(paths.out)
    assert values["FINEPDFS_DB"] == str(paths.database)
    assert values["FINEPDFS_ENGINE"] == "docling"
    assert values["FINEPDFS_ENGINE_COMMAND"] == "/opt/docling"
    assert values["FINEPDFS_MIN_CHARACTERS"] == "500"


def test_restore_replays_the_pinned_manifest_into_the_demo_root(tmp_path) -> None:
    """Restore is the demo's only fetch: it replays the pin and never asks the crawl index."""
    paths = demo.demo_paths(tmp_path / "demo")
    body = (FIXTURES / "warc_payload.bin").read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    pinned = tmp_path / "pinned.json"
    CorpusManifest(
        crawl_id="CC-MAIN-2026-39",
        domain="a.example",
        entries=[
            CorpusEntry(
                url=URL,
                digest=digest,
                file=relative_pdf_path(digest),
                bytes=len(body),
                crawl_id="CC-MAIN-2026-39",
                warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
                offset=1,
                length=len(body),
            )
        ],
    ).save(pinned)

    class FakeSource:
        def list_records(self, query):
            raise AssertionError("restore must replay the pin, not query the index")

        def fetch(self, record):
            return body

    assert demo.restore_demo_corpus(paths, pinned, source=FakeSource()) == 0

    assert (paths.corpus / relative_pdf_path(digest)).read_bytes() == body
    assert CorpusManifest.load(paths.corpus / "manifest.json").urls == {URL}


def test_a_run_keeps_the_warm_state_and_writes_only_under_the_demo_root(
    tmp_path, capsys, monkeypatch
) -> None:
    paths = demo.demo_paths(tmp_path / "demo")
    _write_corpus(paths.corpus)
    marker = paths.root / "warm-cache-marker"
    marker.write_text("this file is the proof a run does not wipe the root")
    monkeypatch.delenv("FINEPDFS_OUT", raising=False)

    engine = CountingEngine()
    assert demo.run_demo(paths, runtime=_runtime(paths, engine)) == 0

    assert marker.exists(), "a plain run must not delete the demo state; only an explicit reset may"
    assert "FINEPDFS_OUT" not in os.environ, "the demo's environment must not outlive the run"
    assert (paths.out / "run-summary.json").exists()
    assert (paths.out / "records").is_dir()
    summary = json.loads(paths.summary.read_text())
    assert summary["extraction"] == {"requested": 1, "calls": 1, "reused": 0}
    assert "run" not in summary, "no timestamp or duration in the declared summary"
    observed = json.loads((paths.out / "last-run.json").read_text())
    assert observed["observed"] is True
    assert observed["elapsed_seconds"] >= 0
    assert "run summary:" in capsys.readouterr().out
    assert engine.calls == 1


def test_reset_removes_only_the_demo_state_and_says_what_it_removed(tmp_path) -> None:
    paths = demo.demo_paths(tmp_path / "demo")
    _write_corpus(paths.corpus)
    paths.out.mkdir(parents=True)
    paths.database.mkdir()
    keep = paths.root / "notes.txt"
    keep.write_text("not demo state")

    removed = demo.reset_demo(paths)

    assert set(removed) == {paths.corpus, paths.out, paths.database}
    assert not paths.corpus.exists() and not paths.out.exists() and not paths.database.exists()
    assert keep.exists(), "reset must not delete what it does not own"
    assert paths.root.exists()


def test_a_run_without_a_restored_corpus_refuses_and_says_how_to_restore(tmp_path, capsys) -> None:
    paths = demo.demo_paths(tmp_path / "demo")

    assert demo.run_demo(paths) == 2

    assert "restore" in capsys.readouterr().err.lower()
    assert not paths.out.exists()


def test_the_demo_runs_the_same_command_through_the_flow_without_a_second_writer(tmp_path) -> None:
    """run_demo is the documented entry point; the flow still owns every output."""
    paths = demo.demo_paths(tmp_path / "demo")
    _write_corpus(paths.corpus)
    engine = CountingEngine()
    demo.run_demo(paths, runtime=_runtime(paths, engine))

    summary = json.loads(paths.summary.read_text())
    record = json.loads(next((paths.out / "records").glob("*.json")).read_text())
    assert record["url"] == URL
    assert summary["published"]["documents"] == 1
    assert summary["exclusions"]["total"] == 0


def test_the_demo_restore_reuses_the_restore_corpus_implementation(tmp_path, monkeypatch) -> None:
    """One restore implementation: both commands dispatch to the same function, neither replays."""
    from finepdfs.interfaces import cli, restore

    calls = []

    def shared(manifest_path, corpus_dir, *, source=None):
        calls.append((manifest_path, corpus_dir, source))
        return 0

    monkeypatch.setattr(demo, "restore_pinned_corpus", shared)
    monkeypatch.setattr(cli, "restore_pinned_corpus", shared)
    paths = demo.demo_paths(tmp_path / "demo")
    pinned = tmp_path / "pinned.json"
    source = object()
    other_corpus = tmp_path / "cli-corpus"

    assert demo.restore_demo_corpus(paths, pinned, source=source) == 0
    assert (
        cli.main(["restore-corpus", "--manifest", str(pinned), "--corpus", str(other_corpus)]) == 0
    )

    assert calls[0] == (pinned, paths.corpus, source)
    assert calls[1][0] == pinned
    assert calls[1][1] == other_corpus
    assert restore.restore_pinned_corpus.__module__ == "finepdfs.interfaces.restore"


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_restore_defaults_to_the_manifest_that_ships_with_the_package(
    tmp_path, monkeypatch
) -> None:
    """A wheel install has no `docs/demo/`, so the pinned manifest must arrive as package data."""
    captured = []

    def shared(manifest_path, corpus_dir, *, source=None):
        captured.append(Path(manifest_path))
        return 0

    monkeypatch.setattr(demo, "restore_pinned_corpus", shared)
    paths = demo.demo_paths(tmp_path / "demo")

    assert demo.restore_demo_corpus(paths) == 0

    (manifest_path,) = captured
    assert manifest_path.is_file(), "the default manifest must be readable where the package is installed"
    assert manifest_path.name == demo.MANIFEST_RESOURCE
    assert CorpusManifest.load(manifest_path).entries, "the packaged manifest lists the pinned corpus"
    package_root = Path(demo.__file__).resolve().parent.parent
    assert package_root in manifest_path.parents, (
        "the default manifest resolves inside the installed package, not above it in a checkout"
    )


def test_the_default_state_root_is_writable_state_not_a_position_in_the_module_path(
    tmp_path, monkeypatch
) -> None:
    """`--root` exists for exactly this: the default must be writable wherever finepdfs is installed."""
    working_directory = tmp_path / "work"
    working_directory.mkdir()
    monkeypatch.chdir(working_directory)
    monkeypatch.delenv(demo.DEMO_ROOT_ENV, raising=False)

    paths = demo.demo_paths()

    assert paths.root == working_directory / ".demo", "the default lives beside the caller, not site-packages"
    assert PROJECT_ROOT not in paths.root.parents
    assert paths.corpus == paths.root / "corpus"

    configured = tmp_path / "state"
    monkeypatch.setenv(demo.DEMO_ROOT_ENV, str(configured))
    assert demo.demo_paths().root == configured, "an explicit root wins over the current directory"


def test_the_built_wheel_carries_the_pinned_manifest_as_package_data(tmp_path) -> None:
    """The packaging test for finding 3: the wheel, not the checkout, is what `pip install` gets."""
    hatchling = pytest.importorskip("hatchling.build")

    wheel = hatchling.build_wheel(str(tmp_path), str(PROJECT_ROOT))

    with zipfile.ZipFile(tmp_path / wheel) as archive:
        names = set(archive.namelist())
    assert f"finepdfs/data/{demo.MANIFEST_RESOURCE}" in names, (
        "the pinned manifest is package data, so an installed demo can restore it"
    )
    assert "finepdfs/interfaces/demo.py" in names
