"""Dependency direction: the domain declares the ports, adapters implement them, nothing points outward.

``DeclaredPageReader`` was declared in ``application/page_reads.py`` while
``infrastructure/publishing/disk.py`` imported the key pattern from that same module, so the output
adapter depended on the use case that consumed it. A port lives in ``domain/ports.py``, which is the repo
convention (CODING_STANDARDS.md, "a port's docstring is true for every adapter").

The CocoIndex flow is a composition root and may call use cases; an output adapter that only reads and
writes declared files may not, because that is the direction the review found inverted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import finepdfs.domain.ports as ports
import finepdfs.infrastructure.publishing as publishing


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_the_declared_page_reader_port_is_declared_in_the_domain() -> None:
    assert hasattr(ports, "DeclaredPageReader")


def test_the_publishing_adapter_does_not_import_the_application_or_interface_layers() -> None:
    root = Path(publishing.__file__).parent
    offenders = [
        f"{path.relative_to(root)} imports {module}"
        for path in sorted(root.rglob("*.py"))
        for module in sorted(_imported_modules(path))
        if module.startswith(("finepdfs.application", "finepdfs.interfaces"))
    ]

    assert offenders == []
