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
