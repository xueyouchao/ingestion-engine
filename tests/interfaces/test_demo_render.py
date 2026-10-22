"""The rendered document's allowlist: structural markup only, and honest when it is not installed.

The document pane is the one place the demo turns text into HTML, and that text is model output. So the
allowlist is structural -- headings, paragraphs, lists, emphasis, code, blockquotes, rules and tables --
and there is deliberately no navigation: no link, no image, no attribute an event could run from. Math
stays text. The renderer and sanitizer are the pinned ``[demo]`` pair; this module proves the pair's
output and that the demo says "not installed" instead of falling back to unescaped text.
"""

from __future__ import annotations

import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

import finepdfs.interfaces.demo_render as demo_render
from finepdfs.domain.document import PageText
from finepdfs.interfaces.demo_job import DocumentView

REPO = Path(__file__).resolve().parents[2]

# What a document fragment may carry: the pane's own wrapper (article, section), the page labels (h2),
# and the structural markup the sanitizer admits. ``style``, ``script``, ``iframe`` and ``img`` are
# absent by construction, and so is ``a`` -- a link would be navigation the model wrote.
ALLOWED_TAGS = {
    "article",
    "section",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "ul",
    "ol",
    "li",
    "em",
    "strong",
    "code",
    "pre",
    "blockquote",
    "hr",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "br",
    "span",
}


class Tags(HTMLParser):
    """Every tag and attribute the fragment carries, whatever the renderer emitted."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _elements(fragment: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = Tags()
    parser.feed(fragment)
    return parser.elements


def _text(fragment: str) -> str:
    """The fragment's visible text, with entities decoded -- what a reader would actually see."""
    parts: list[str] = []

    class Text(HTMLParser):
        def handle_data(self, data: str) -> None:
            parts.append(data)

    parser = Text()
    parser.feed(fragment)
    return "".join(parts)


def test_a_document_fragment_carries_each_completed_page_and_a_skeleton_for_the_rest() -> None:
    view = DocumentView(pages=(PageText(1, "# One\n\nFirst body."),), total=3)

    fragment = demo_render.render_document(view)

    assert fragment.startswith('<article class="document"')
    assert 'data-pages="3"' in fragment
    assert 'data-completed="1"' in fragment
    assert '<section class="document-page" data-page="1" data-state="complete">' in fragment
    assert fragment.count('data-state="complete"') == 1
    assert 'data-page="2" data-state="pending"' in fragment
    assert 'data-page="3" data-state="pending"' in fragment
    assert fragment.index('data-page="1"') < fragment.index('data-page="2"') < fragment.index('data-page="3"')
    assert '<h1>One</h1>' in fragment
    assert '<p>First body.</p>' in fragment
    assert '<p class="skeleton" data-state="pending">Page 2 is still being read.</p>' in fragment


def test_a_page_count_the_job_has_not_learned_yet_is_not_invented() -> None:
    fragment = demo_render.render_document(DocumentView(pages=(PageText(1, "only page so far"),)))

    assert "data-pages=" not in fragment
    assert 'data-completed="1"' in fragment
    assert 'data-page="1" data-state="complete"' in fragment
    assert 'data-page="2" data-state="pending"' in fragment, "the next page is the one being read"


def test_an_empty_document_says_so_without_inventing_a_page() -> None:
    fragment = demo_render.render_document(DocumentView())

    assert 'data-completed="0"' in fragment
    assert "<section" not in fragment
    assert '<p class="document-empty" data-state="pending">No page has completed yet.</p>' in fragment


def test_the_structural_markup_a_page_can_carry_renders_as_structure() -> None:
    page = PageText(
        1,
        "# Heading\n\nA paragraph with *emphasis*, **weight** and `code`.\n\n"
        "- one\n- two\n\n1. first\n2. second\n\n> a quote\n\n---\n\n"
        "```\na block\n```\n\n| Region | Total |\n| --- | --- |\n| North | 12 |\n",
    )

    fragment = demo_render.render_document(DocumentView(pages=(page,), total=1))

    for tag in ("h1", "p", "em", "strong", "code", "ul", "ol", "li", "blockquote", "hr", "pre", "table",
                "thead", "tbody", "tr", "th", "td"):
        assert f"<{tag}" in fragment, f"{tag} is in the allowlist"
    assert _text(fragment).count("North") == 1


def test_tables_keep_colspan_and_rowspan_but_no_attribute_that_could_run() -> None:
    html = (
        '<table><tr><td colspan="2" rowspan="3" onclick="alert(1)" style="color:red">kept</td></tr></table>'
        "<script>alert(2)</script><iframe src=\"https://evil.example\"></iframe>"
    )

    sanitized = demo_render.sanitize_fragment(html)

    assert 'colspan="2"' in sanitized
    assert 'rowspan="3"' in sanitized
    assert "onclick" not in sanitized
    assert "style=" not in sanitized
    assert "<script" not in sanitized
    assert "alert(2)" not in sanitized, "a script's content is removed with it, not shown as text"
    assert "<iframe" not in sanitized


def test_a_hostile_page_renders_inert_markup_only() -> None:
    hostile = PageText(
        1,
        "# A page the model wrote\n\n"
        "<script>alert('script')</script>\n\n"
        '<img src="x" onerror="alert(\'onerror\')">\n\n'
        "[a link](https://evil.example/steal)\n\n"
        "[click](javascript:alert('target'))\n\n"
        "![an image](https://evil.example/pixel.png)\n",
    )

    fragment = demo_render.render_document(DocumentView(pages=(hostile,), total=1))
    elements = _elements(fragment)

    assert {tag for tag, _ in elements} <= ALLOWED_TAGS
    for tag, attrs in elements:
        assert tag not in {"script", "style", "iframe", "img", "a", "object", "embed", "form"}
        for name, value in attrs.items():
            assert not name.lower().startswith("on"), f"{tag}[{name}] is an event handler"
            assert name.lower() not in {"href", "src", "action", "srcdoc"}
            assert "javascript:" not in (value or "").lower()
    assert "alert('onerror')" not in "".join(value or "" for _, attrs in elements for value in attrs.values())
    visible = _text(fragment)
    assert "a link" in visible, "the text survives; the navigation does not"
    assert "click" in visible


def test_math_stays_text_until_there_is_a_reason_to_ship_a_math_renderer() -> None:
    page = PageText(1, "The relation $E = mc^2$ is inline, and $$\\int_0^1 x\\,dx$$ is display math.")

    fragment = demo_render.render_document(DocumentView(pages=(page,), total=1))

    assert "$E = mc^2$" in _text(fragment)
    assert "\\int_0^1" in _text(fragment)
    assert "<math" not in fragment


def test_marker_residue_renders_as_the_residue_it_is() -> None:
    """The demo contributes no second cleanup; a marker the adapter did not strip is visible as text."""
    page = PageText(1, "<|det|>table [10,20,30,40]<|/det|>\n\nThe row after the marker.")

    fragment = demo_render.render_document(DocumentView(pages=(page,), total=1))

    assert "<|det|>table [10,20,30,40]<|/det|>" in _text(fragment)


def test_rendering_says_it_is_not_installed_rather_than_rendering_unescaped(monkeypatch) -> None:
    def missing():
        raise ImportError("No module named 'nh3'")

    monkeypatch.setattr(demo_render, "_dependencies", missing)

    assert demo_render.rendering_installed() is False
    with pytest.raises(demo_render.RenderingNotInstalled, match="not installed"):
        demo_render.render_document(DocumentView(pages=(PageText(1, "text"),), total=1))


def test_the_server_can_be_imported_without_the_demo_extra() -> None:
    """The base install never needs the pair: only the document endpoint imports it, and lazily."""
    code = (
        "import sys; import finepdfs.interfaces.demo_server; "
        "assert 'markdown_it' not in sys.modules, 'markdown_it imported at import time'; "
        "assert 'nh3' not in sys.modules, 'nh3 imported at import time'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr