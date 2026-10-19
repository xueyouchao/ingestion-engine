"""The deploy slice stays outside the package: no vendor dependency, no vendor import, no second port.

ADR-0009's consequence is structural: the base install stays vendor-free and nothing under
``src/finepdfs`` imports Modal. This is the mechanical check, and it also pins the deploy requirements
file to the CLI version this slice was written against.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "finepdfs"
MODAL_REQUIREMENTS = ROOT / "deploy" / "requirements-modal.txt"
VENDORS = ("modal", "runpod")


def test_the_base_install_stays_vendor_free() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = " ".join(project["dependencies"]).lower()

    for vendor in VENDORS:
        assert vendor not in dependencies, f"{vendor} belongs in deploy/requirements, not the base install"


def test_the_modal_requirements_pin_the_installed_cli_version() -> None:
    pins = [
        line.strip()
        for line in MODAL_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert pins == ["modal==1.5.5"], "the deploy environment is pinned exactly; the CLI reports 1.5.5"


def test_no_core_module_imports_a_gpu_vendor_sdk() -> None:
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            offenders.extend(
                f"{path.relative_to(ROOT)} imports {module}"
                for module in modules
                if module.split(".")[0] in VENDORS
            )

    assert offenders == []


def test_the_vendor_scripts_import_modal_lazily() -> None:
    """The offline suite runs without Modal installed because the scripts import it inside a call."""
    offenders = []
    for name in ("start.py", "stop.py", "serve.py", "warm.py"):
        path = ROOT / "deploy" / "modal" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            modules = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            offenders.extend(
                f"{path.relative_to(ROOT)} imports {module} at module scope"
                for module in modules
                if module.split(".")[0] == "modal"
            )

    assert offenders == []


def test_the_vendor_code_lives_in_deploy_and_not_in_the_package() -> None:
    assert (ROOT / "deploy" / "gpu").is_dir()
    assert (ROOT / "deploy" / "modal").is_dir()
    assert not (PACKAGE / "deploy").exists()
    assert not list(PACKAGE.rglob("start.py"))
    assert not list(PACKAGE.rglob("stop.py"))
