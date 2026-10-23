"""The rendered document's allowlist: structural markup only, and honest when it is not installed.

The document pane is the one place the demo turns text into HTML, and that text is model output. So the
allowlist is structural -- headings, paragraphs, lists, emphasis, code, blockquotes, rules and tables --
and there is deliberately no navigation: no link, no image, no attribute an event could run from. Math
stays text. The renderer and sanitizer are the pinned ``[demo]`` pair; this module proves the pair's
output and that the demo says "not installed" instead of falling back to unescaped text.
"""

from __future__ import annotations

import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

import finepdfs.interfaces.demo_render as demo_render
from finepdfs.domain.document import PageText
from finepdfs.interfaces.demo_job import DocumentView

REPO = Path(__file__).resolve().parents[2]

# What a document fragment may carry beyond the module's own allowlist: the pane's trusted wrapper. The
# vocabulary itself is read from ``demo_render`` -- a second hand-kept copy could admit a tag the module
# does not, or miss one it grows.
WRAPPER_TAGS = frozenset({"article", "section"})


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


def _page_numbers(fragment: str) -> list[str]:
    return re.findall(r'<section class="document-page" data-page="(\d+)"', fragment)


def _states(fragment: str) -> list[str]:
    return re.findall(r'<section class="document-page" data-page="\d+" data-state="(\w+)"', fragment)


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
    """With no count, the pane shows the pages the read produced and claims no page it has not seen."""
    fragment = demo_render.render_document(DocumentView(pages=(PageText(1, "only page so far"),)))

    assert "data-pages=" not in fragment
    assert 'data-completed="1"' in fragment
    assert 'data-page="1" data-state="complete"' in fragment
    assert 'data-state="pending"' not in fragment, "the next page is not a page until the read reports it"


def test_pages_that_arrive_out_of_order_land_in_numeric_order_and_no_later_page_is_claimed() -> None:
    """The pane reads the shared ordering rule; the last page is the highest it has, not one more."""
    view = DocumentView(pages=(PageText(10, "the tenth page"), PageText(2, "the second page")))

    fragment = demo_render.render_document(view)

    assert _page_numbers(fragment) == [str(number) for number in range(1, 11)], "numeric, not arrival order"
    assert fragment.index("the second page") < fragment.index("the tenth page")
    assert _states(fragment) == ["pending"] + ["complete"] + ["pending"] * 7 + ["complete"]
    assert "Page 11" not in fragment, "the page after the highest one is not invented"
    assert "data-pages=" not in fragment, "the pane claims no count the job has not learned"


def test_a_page_beyond_the_count_is_still_drawn_and_its_text_is_not_dropped() -> None:
    """A finished Reading's total is ``len(pages)``, so a lane that reported pages 1 and 3 makes two pages.

    Page 3 was really read: it must be drawn, in its own place, rather than vanish behind the count that
    the Reading's own length supplied.
    """
    view = DocumentView(pages=(PageText(1, "the first page"), PageText(3, "the third page")), total=2)

    fragment = demo_render.render_document(view)

    assert _page_numbers(fragment) == ["1", "2", "3"], "the reported page is not cut off by a smaller count"
    assert "the third page" in _text(fragment), "page 3's words are not dropped"
    assert 'data-page="2" data-state="pending"' in fragment, "the page between them is still a skeleton"
    assert _states(fragment) == ["complete", "pending", "complete"]
    assert 'data-completed="2"' in fragment, "what was drawn is what the count says completed"


def test_a_failed_read_names_the_pages_it_never_read_instead_of_promising_them() -> None:
    """ADR-0014: a read that failed read nothing more; the pane says so rather than still reading."""
    view = DocumentView(
        pages=(PageText(1, "the page that completed before the failure"),), total=3, failed=True
    )

    fragment = demo_render.render_document(view)

    assert _page_numbers(fragment) == ["1", "2", "3"]
    assert 'data-page="1" data-state="complete"' in fragment
    assert "the page that completed before the failure" in fragment, "the pages it completed are kept"
    assert 'data-page="2" data-state="unread"' in fragment
    assert 'data-page="3" data-state="unread"' in fragment
    assert "still being read" not in fragment, "a failed read is not still reading anything"
    assert '<p class="unread" data-state="unread">Page 2 was not read: the read failed.</p>' in fragment


def test_a_failed_read_with_nothing_completed_calls_no_page_still_being_read() -> None:
    """Nothing completed and no count is known: the pane says the read failed, not that it is reading."""
    unnamed = demo_render.render_document(DocumentView(failed=True))
    assert '<p class="document-empty" data-state="unread">No page was read: the read failed.</p>' in unnamed
    assert "still being read" not in unnamed

    counted = demo_render.render_document(DocumentView(total=2, failed=True))
    assert _page_numbers(counted) == ["1", "2"]
    assert _states(counted) == ["unread", "unread"]
    assert "still being read" not in counted


def test_an_empty_document_says_so_without_inventing_a_page() -> None:
    fragment = demo_render.render_document(DocumentView())

    assert 'data-completed="0"' in fragment
    assert "<section" not in fragment
    assert '<p class="document-empty" data-state="pending">No page has completed yet.</p>' in fragment


def test_the_page_fragment_is_exported_so_a_second_pane_shows_the_same_bytes() -> None:
    """The bundle's document pane renders pages through this function, not beside it.

    One projection, one allowlist: a page fragment the demo pane inserts and a page fragment the bundle
    inserts are the same string for the same Page Text, and both come from the numeric ordering rule.
    """
    pages = (PageText(2, "the second page"), PageText(1, "# The first page"))

    fragments = demo_render.render_pages(pages)

    assert _page_numbers("".join(fragments)) == ["1", "2"], "one fragment per page, in numeric order"
    assert fragments[0].startswith(
        '<section class="document-page" data-page="1" data-state="complete">\n'
        '<h2 class="document-page-number">Page 1</h2>\n'
    ), "the fragment carries the page vocabulary, with no wrapper of its own"
    assert fragments[0].endswith("</section>")
    assert "<h1>The first page</h1>" in fragments[0], "the page is the projection's, allowlist and all"
    article = demo_render.render_document(DocumentView(pages=pages, total=2))
    for fragment in fragments:
        assert fragment in article, "the pane and the bundle insert the same bytes"


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


def test_markup_a_page_carries_reaches_the_pane_as_text_not_as_an_element() -> None:
    """Page Text is markdown, never raw HTML: an attribute the model wrote has no element to ride in."""
    page = PageText(
        1,
        '<table><tr><td colspan="2" onclick="alert(1)" style="color:red">a cell</td></tr></table>',
    )

    fragment = demo_render.render_document(DocumentView(pages=(page,), total=1))

    assert "td" not in {tag for tag, _ in _elements(fragment)}, "the page's own markup is not an element"
    assert "<td" not in fragment and "<table>" not in fragment, "it is escaped text, not markup"
    assert "a cell" in _text(fragment), "the text survives; the markup around it does not"


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

    assert {tag for tag, _ in elements} <= demo_render.ALLOWED_TAGS | WRAPPER_TAGS
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