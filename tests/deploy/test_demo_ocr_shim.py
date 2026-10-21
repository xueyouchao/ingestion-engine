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
import re
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


def _run(
    shim_arguments: list[str], path: Path, **extra: str
) -> subprocess.CompletedProcess[str]:
    # ``PATH`` is only the directory under test: the shim must find the stub there and nothing else, so
    # "tesseract is missing" is a state this test can actually reach on a machine that has tesseract.
    environment = {**os.environ, "PATH": str(path), **extra}
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


def test_the_shim_answers_only_the_subcommand_the_engine_calls(tmp_path: Path) -> None:
    """Anything else would dress an unknown request up as a successful read."""
    images = _pages(tmp_path, 1)
    stub = _stub_tesseract(tmp_path)

    result = _run(["parse", "-i", str(images), "--save_path", str(tmp_path / "results")], stub)

    assert result.returncode == 2
    assert "unknown subcommand 'parse'" in result.stderr
    assert not list((tmp_path / "results").glob("*_res.json"))


def test_a_hung_tesseract_fails_the_page_instead_of_outliving_the_read(tmp_path: Path) -> None:
    """The engine's bound kills this process, not the tesseract it spawned."""
    images = _pages(tmp_path, 1)
    binary = tmp_path / "bin"
    binary.mkdir()
    slow = binary / "tesseract"
    # An absolute path: the shim runs with a PATH that holds nothing but this stub directory.
    slow.write_text("#!/bin/sh\n/bin/sleep 30\n", encoding="utf-8")
    slow.chmod(0o755)

    result = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "results"), "--timeout", "0.2"],
        binary,
    )

    assert result.returncode == 1
    assert "did not finish page-0001.png within" in result.stderr
    assert "0.2s budget" in result.stderr, "the message names the budget it was given"


def test_the_bundle_rebuild_hint_keeps_the_live_link() -> None:
    """An operator who rebuilds from these instructions must keep the two pages pointing at each other."""
    runbook = (ROOT / "deploy" / "bundle" / "run.sh").read_text(encoding="utf-8")
    readme = (ROOT / "deploy" / "bundle" / "README.md").read_text(encoding="utf-8")

    assert "--live /live/" in runbook
    assert "--live /live/" in readme
    assert "--link https://$HOST/" in runbook, "the hint follows NAME/HOST, not a frozen hostname"


def test_the_shim_refuses_a_flag_it_does_not_know(tmp_path: Path) -> None:
    """It stands in for the engine's command, so a contract change must fail loudly rather than drift."""
    images = _pages(tmp_path, 1)
    stub = _stub_tesseract(tmp_path)

    result = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "r"), "--layout_model", "x"],
        stub,
    )

    assert result.returncode == 2
    assert "unrecognized arguments: --layout_model x" in result.stderr, "argparse names the flag"
    assert not list((tmp_path / "r").glob("*_res.json"))


def test_the_subcommand_is_judged_before_the_hosts_tooling(tmp_path: Path) -> None:
    """A wrong invocation is a contract error, not a missing tesseract, and says so even where it is absent."""
    images = _pages(tmp_path, 1)
    empty = tmp_path / "empty-bin"
    empty.mkdir()

    result = _run(["parse", "-i", str(images), "--save_path", str(tmp_path / "r")], empty)

    assert result.returncode == 2
    assert "unknown subcommand 'parse'" in result.stderr


def test_the_read_is_budgeted_inside_the_engines_own_bound(tmp_path: Path) -> None:
    """A fixed per-page bound, or one that sums to the bound, lets the engine's kill answer first."""
    images = _pages(tmp_path, 2)
    binary = tmp_path / "bin"
    binary.mkdir()
    slow = binary / "tesseract"
    slow.write_text("#!/bin/sh\n/bin/sleep 30\n", encoding="utf-8")
    slow.chmod(0o755)

    result = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "r")],
        binary,
        FINEPDFS_ENGINE_TIMEOUT="0.4",
    )

    assert result.returncode == 1
    assert "did not finish page-0001.png within" in result.stderr
    assert "of its 0.4s budget" in result.stderr, "the budget is the engine's bound this run was given"


def test_non_ascii_page_text_survives_a_plain_c_locale(tmp_path: Path) -> None:
    """Tesseract writes UTF-8; decoding it as the host's locale would raise on the text this lane is for."""
    images = _pages(tmp_path, 1)
    binary = tmp_path / "bin"
    binary.mkdir()
    stub = binary / "tesseract"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' '扫描 页'\n", encoding="utf-8")
    stub.chmod(0o755)

    result = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "r")],
        binary,
        LC_ALL="C",
        PYTHONUTF8="0",
        PYTHONCOERCECLOCALE="0",
    )

    assert result.returncode == 0, result.stderr
    written = json.loads((tmp_path / "r" / "page-0001_res.json").read_text(encoding="utf-8"))
    assert written["res"]["parsing_res_list"][0]["block_content"] == "扫描 页"


def test_the_shims_default_bound_is_the_engines_own() -> None:
    """A second copy of a default drifts: this one must equal `environment.DEFAULT_TIMEOUT_SECONDS`."""
    from finepdfs.infrastructure.engines import environment

    source = SHIM.read_text(encoding="utf-8")
    stated = re.search(r"^DEFAULT_ENGINE_TIMEOUT = ([0-9.]+)$", source, re.M)

    assert stated is not None
    assert float(stated.group(1)) == environment.DEFAULT_TIMEOUT_SECONDS


def test_a_caller_cannot_outbudget_the_engine(tmp_path: Path) -> None:
    """Past the engine's own bound the runner kills the shim before it can name the page."""
    images = _pages(tmp_path, 1)
    binary = tmp_path / "bin"
    binary.mkdir()
    slow = binary / "tesseract"
    slow.write_text("#!/bin/sh\n/bin/sleep 30\n", encoding="utf-8")
    slow.chmod(0o755)

    result = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "r"), "--timeout", "600"],
        binary,
        FINEPDFS_ENGINE_TIMEOUT="0.4",
    )

    assert result.returncode == 1
    assert "of its 0.4s budget" in result.stderr, "clamped to the bound, not 540s"


@pytest.mark.parametrize("budget", ["nan", "0", "-1"])
def test_an_unusable_budget_is_named_with_its_source(tmp_path: Path, budget: str) -> None:
    """``float`` accepts these, and NaN would reach the subprocess timeout and end in a traceback."""
    images = _pages(tmp_path, 1)
    stub = _stub_tesseract(tmp_path)

    result = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "r"), "--timeout", budget],
        stub,
    )

    assert result.returncode == 1
    assert "no time to read anything" in result.stderr
    assert "--timeout" in result.stderr, "the message names where the unusable budget came from"
    assert not list((tmp_path / "r").glob("*_res.json"))


def test_an_infinite_bound_is_refused_because_nothing_can_wait_on_it(tmp_path: Path) -> None:
    """``subprocess.run(timeout=inf)`` raises OverflowError, so an infinite bound is not "no bound"."""
    images = _pages(tmp_path, 1)
    stub = _stub_tesseract(tmp_path)

    result = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "r")],
        stub,
        FINEPDFS_ENGINE_TIMEOUT="inf",
    )

    assert result.returncode == 1
    assert "the budget from FINEPDFS_ENGINE_TIMEOUT is inf" in result.stderr
    assert not list((tmp_path / "r").glob("*_res.json"))


@pytest.mark.parametrize("budget", ["inf", "-inf", "nan", "0", "-1"])
def test_one_rule_for_both_sources(tmp_path: Path, budget: str) -> None:
    """Whether it arrives from the environment or the flag, a budget must be a positive finite number."""
    images = _pages(tmp_path, 1)
    stub = _stub_tesseract(tmp_path)

    from_environment = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "env")],
        stub,
        FINEPDFS_ENGINE_TIMEOUT=budget,
    )
    # ``--timeout=-inf``: argparse reads a bare ``-inf`` as a flag, which is a usage error and not the point.
    from_flag = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "flag"), f"--timeout={budget}"],
        stub,
    )

    for result, source in ((from_environment, "FINEPDFS_ENGINE_TIMEOUT"), (from_flag, "--timeout")):
        assert result.returncode == 1, result.stderr
        assert f"the budget from {source} is {budget}" in result.stderr


def test_a_nan_engine_bound_cannot_widen_an_explicit_budget(tmp_path: Path) -> None:
    """``min(30, nan)`` is 30 in Python: an unusable environment bound must not pass the caller's read."""
    images = _pages(tmp_path, 1)
    stub = _stub_tesseract(tmp_path)

    result = _run(
        ["doc_parser", "-i", str(images), "--save_path", str(tmp_path / "r"), "--timeout", "30"],
        stub,
        FINEPDFS_ENGINE_TIMEOUT="nan",
    )

    assert result.returncode == 1
    assert "the budget from FINEPDFS_ENGINE_TIMEOUT is nan" in result.stderr
