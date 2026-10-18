"""The install surface: the base suite stays model-free, and OCR is an optional pinned extra.

Docling is the CPU Engine the demo runbook validates. It is deliberately not a base dependency: the
default suite needs no model, no weights and no download, and the base install must stay that way.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _project() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def test_the_base_install_stays_model_free() -> None:
    dependencies = " ".join(_project()["dependencies"]).lower()

    assert "docling" not in dependencies
    assert "torch" not in dependencies
    assert "fasttext" not in dependencies


def test_docling_is_a_pinned_optional_extra_not_an_unpinned_torch() -> None:
    extras = _project()["optional-dependencies"]

    assert extras["docling"] == ["docling==2.140.0"]
    for name, pins in extras.items():
        for pin in pins:
            assert not pin.lower().startswith("torch"), (
                f"the {name} extra must not carry an unpinned torch; document the CPU install instead"
            )


def test_the_base_install_pins_the_working_versions() -> None:
    """The spec says pin the working dependencies: an unpinned floor can reproduce different bytes."""
    assert _project()["dependencies"] == ["cocoindex==1.0.25", "pymupdf==1.28.2"]


DEMO_REQUIREMENTS = Path(__file__).resolve().parents[1] / "docs" / "demo" / "requirements-demo.txt"
CPU_WHEEL_INDEX = "https://download.pytorch.org/whl/cpu"


def test_the_demo_requirements_record_the_working_environment() -> None:
    """A fresh install must be able to reproduce page text, chunk ids and citations."""
    recorded = DEMO_REQUIREMENTS.read_text(encoding="utf-8")

    for pin in ("docling==2.140.0", "torch==2.10.0+cpu", "easyocr==1.7.2"):
        assert pin in recorded, f"the demo environment must record {pin}"
    assert CPU_WHEEL_INDEX in recorded, "the CPU wheel index is part of the install, not folklore"
    assert "docling" not in " ".join(_project()["dependencies"]).lower(), (
        "recording the demo environment must not make the base install model-bearing"
    )
