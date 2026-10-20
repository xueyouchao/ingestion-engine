"""The demo's local OCR shim: the contract the CLI engine calls it with, and the words it fails with.

The shim is not a library: it is the command `FINEPDFS_ENGINE_COMMAND` points at on the VPS, so the only
way to keep it honest is to run it the way the engine does -- the same argument list, a page-image
directory, and a stubbed `tesseract` on `PATH` instead of a real OCR binary. Its output is then parsed by
the very function the engine uses, which is the assertion that matters: a renamed flag or a differently
shaped `*_res.json` would break the site and nothing else would notice.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from finepdfs.infrastructure.engines.paddleocr_vl import page_from_paddle_result

ROOT = Path(__file__).resolve().parents[2]
SHIM = ROOT / "deploy" / "demo" / "ocr-shim"


def _pages(tmp_path: Path, count: int) -> Path:
    images = tmp_path / "pages"
    images.mkdir()
    for number in range(1, count + 1):
        (images / f"page-{number:04d}.png").write_bytes(b"not a real png: the stub never reads it")
    return images


def _stub_tesseract(tmp_path: Path, *, output: str = "Scanned page", status: int = 0) -> Path:
    """A ``tesseract`` on PATH that answers the way the real one does: text on stdout, 0 on success."""
    binary = tmp_path / "bin"
    binary.mkdir()
    stub = binary / "tesseract"
    stub.write_text(
        "#!/bin/sh\n"
        f'if [ {status} -ne 0 ]; then\n'
        '  echo "tesseract failed on purpose" >&2\n'
        f"  exit {status}\n"
        "fi\n"
        f'echo "{output} $(basename "$1")"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return binary


def _run(shim_arguments: list[str], path: Path) -> subprocess.CompletedProcess[str]:
    # ``PATH`` is only the directory under test: the shim must find the stub there and nothing else, so
    # "tesseract is missing" is a state this test can actually reach on a machine that has tesseract.
    environment = {**os.environ, "PATH": str(path)}
    return subprocess.run(
        [sys.executable, str(SHIM), *shim_arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def test_the_shim_writes_one_paddle_result_per_page_the_engine_can_read(tmp_path: Path) -> None:
    images = _pages(tmp_path, 3)
    output = tmp_path / "results"
    stub = _stub_tesseract(tmp_path)

    result = _run(
        ["doc_parser", "-i", str(images), "--pipeline_version", "v1.6", "--save_path", str(output)],
        stub,
    )

    assert result.returncode == 0, result.stderr
    files = sorted(output.glob("*_res.json"))
    assert len(files) == 3, "one result per rendered page, which is what the engine collects"
    pages = [
        page_from_paddle_result(json.loads(path.read_text(encoding="utf-8")), fallback_number=index)
        for index, path in enumerate(files, start=1)
    ]
    assert [page.number for page in pages] == [1, 2, 3]
    assert all(page.text.startswith("Scanned page") for page in pages)


def test_the_shim_says_so_when_tesseract_is_missing(tmp_path: Path) -> None:
    """The engine quotes the last stderr lines as the read's reason, so it must not be a traceback."""
    images = _pages(tmp_path, 1)
    empty = tmp_path / "empty-bin"
    empty.mkdir()

    result = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "results")],
        empty,
    )

    assert result.returncode == 1
    assert "tesseract is not installed" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "results").exists() or not list((tmp_path / "results").glob("*_res.json"))


def test_the_shim_reports_a_failing_ocr_rather_than_writing_an_empty_page(tmp_path: Path) -> None:
    images = _pages(tmp_path, 1)
    stub = _stub_tesseract(tmp_path, status=1)

    result = _run(["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "results")], stub)

    assert result.returncode == 1
    assert "tesseract failed" in result.stderr
    assert "failed on purpose" in result.stderr


def test_the_shim_refuses_a_directory_with_no_pages(tmp_path: Path) -> None:
    empty = tmp_path / "pages"
    empty.mkdir()
    stub = _stub_tesseract(tmp_path)

    result = _run(["doc_parser", "-i", str(empty), "--save_path", str(tmp_path / "results")], stub)

    assert result.returncode == 1
    assert "no page images" in result.stderr


@pytest.mark.parametrize("name", ["ocr-shim"])
def test_the_shim_is_executable(name: str) -> None:
    """The engine runs it by path, so the executable bit is part of the contract."""
    assert os.access(ROOT / "deploy" / "demo" / name, os.X_OK)
