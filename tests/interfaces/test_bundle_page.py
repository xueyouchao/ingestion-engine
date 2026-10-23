"""The reader's third pane: the document as it would be published, rendered or as its markdown source.

The exported bundle is a static directory with no markdown parser in it, so the pane is built from what
``bundle.py`` computed: each page reading's ``html`` is the demo's own projection (``render_pages``), and
the markdown view is the page texts joined by the projection's separator. These tests pin the asset's
markup and styles, then run the page's own pure functions under Node against a payload shaped exactly
like a built one, so the toggle, the numbering and the degraded cases are the browser's behaviour and
not a description of it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from finepdfs.domain.publishing import PAGE_SEPARATOR
from finepdfs.interfaces import bundle_page

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node runs the page's pure script functions")

# Two readings of one document, as a bundle writes them: both read pages 1 and 2, and only one read page
# 3. The fragment is a stand-in with the projection's own page vocabulary, which is what the build emits.
FRAGMENT = (
    '<section class="document-page" data-page="{number}" data-state="complete">\n'
    '<h2 class="document-page-number">Page {number}</h2>\n<p>page {number}</p>\n</section>'
)
TEXTS = {1: "the first page", 2: "the second page", 3: "the third page"}
# The pane's own functions, in the order they build on each other; the toggle and the drawing on top.
PANES = ("documentPages", "documentMarkdown", "documentEmpty", "documentUnrendered", "documentBody")
DRAWING = (*PANES, "renderViews", "renderDocument")
MARKDOWN = PAGE_SEPARATOR.join(TEXTS[number] for number in (1, 2, 3))


def _reading(number: int) -> dict:
    return {"text": TEXTS[number], "chunks": [], "html": FRAGMENT.format(number=number)}


def _payload() -> dict:
    return {
        "dpi": 72,
        "page_separator": PAGE_SEPARATOR,
        "states": [{"label": "paddleocr-vl"}, {"label": "pymupdf"}],
        "documents": {"doc-a": {"url": "https://a.example/a.pdf", "reads": {}}},
        "pages": {
            "doc-a:1": {"readings": {"paddleocr-vl": _reading(1), "pymupdf": _reading(1)}},
            "doc-a:2": {"readings": {"paddleocr-vl": _reading(2), "pymupdf": _reading(2)}},
            "doc-a:3": {"readings": {"paddleocr-vl": _reading(3)}},
        },
        "excluded": {},
    }


def _function(name: str) -> str:
    """The page's source for one top-level function, so Node runs exactly what the browser does."""
    found = re.search(rf"((?:async )?function {name}\(.*?\n\}})", bundle_page.BUNDLE_HTML, re.S)
    assert found is not None, f"the page asset defines {name}()"
    return found.group(1)


def _constant(name: str) -> str:
    """A top-level ``const`` the asset defines, so Node reads the page's own data, not a copy of it."""
    found = re.search(rf"^const {name} = .*?;", bundle_page.BUNDLE_HTML, re.M | re.S)
    assert found is not None, f"the page asset defines the constant {name}"
    return found.group(0)


def _prelude(label: str = "paddleocr-vl", view: str = "rendered") -> str:
    """The page's data and small helpers, plus a DOM stand-in for the nodes renderDocument touches."""
    return f"""
const DATA = {json.dumps(_payload())};
const state = {{ label: {json.dumps(label)}, doc: "doc-a", page: "doc-a:1", find: "", corpus: "", hits: [],
  index: [], view: {json.dumps(view)} }};
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const docOf = (key) => key.split(":")[0];
const pageNo = (key) => Number(key.split(":")[1]);
const pagesOf = (id) => Object.keys(DATA.pages).filter((key) => docOf(key) === id)
  .sort((a, b) => pageNo(a) - pageNo(b));
function element(name) {{
  const own = {{ name, children: [], listeners: {{}}, dataset: {{}}, attrs: {{}}, innerHTML: "",
    appendChild(child) {{ own.children.push(child); return child; }},
    setAttribute(key, value) {{ own.attrs[key] = String(value); }},
    addEventListener(key, fn) {{ own.listeners[key] = fn; }} }};
  // Clearing an element's text clears its children too, as the DOM does -- renderViews relies on it.
  let text = "";
  Object.defineProperty(own, "textContent", {{
    get: () => text,
    set: (value) => {{ text = String(value); if (value === "") own.children = []; }},
  }});
  return own;
}}
const elements = {{}};
for (const id of ["document-count", "document-views", "document-body"]) elements[id] = element(id);
const $ = (sel) => elements[sel.replace("#", "")];
const document = {{ createElement: (tag) => element(tag), querySelector: $ }};
"""


def _run(
    functions: tuple[str, ...], body: str, constants: tuple[str, ...] = (), **prelude
) -> dict:
    """Run one snippet of the page's own JS and read its JSON answer."""
    source = "\n".join(_constant(name) for name in constants) + "\n" + "\n".join(
        _function(name) for name in functions
    )
    script = f"{_prelude(**prelude)}\n{source}\n{body}\n"
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _css() -> str:
    return bundle_page.BUNDLE_HTML.split("<style>", 1)[1].split("</style>", 1)[0]


@requires_node
def test_the_asset_is_a_script_a_browser_can_parse(tmp_path) -> None:
    """A static file has no build step after this one: a syntax error here is a blank page."""
    script = bundle_page.BUNDLE_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    path = tmp_path / "page.js"
    path.write_text(script, encoding="utf-8")

    result = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, result.stderr


def test_the_document_is_the_third_pane_and_stacks_below_the_other_two_on_a_narrow_screen() -> None:
    """The document is read beside the page it was read from, and it is the one on the right."""
    page = bundle_page.BUNDLE_HTML
    panes = [page.index(f'<section class="pane" aria-label="{name}"')
             for name in ("the page", "accepted text", "the document")]
    assert panes == sorted(panes), "the document pane comes after the page image and the accepted text"
    assert page.index('<div class="text"></div>') < panes[2], "the document pane follows the accepted text"

    css = _css()
    wide = css.split("main {", 1)[1].split("}", 1)[0]
    assert wide.count("minmax(0, 1fr)") == 3, "three panes share the row on a wide screen"
    assert "@media (max-width: 900px) { main { grid-template-columns: 1fr; } }" in css, (
        "the existing narrow rule stacks all three"
    )


def test_the_pane_offers_one_heading_one_toggle_and_one_body() -> None:
    page = bundle_page.BUNDLE_HTML
    start = page.index('<section class="pane" aria-label="the document"')
    body = page[start:page.index("</section>", start)]

    assert body.index("<h2>") < body.index('id="document-views"') < body.index('id="document-body"')
    assert 'class="toggle" id="document-views"' in body, "the toggle reuses the controls' button styles"
    assert 'aria-label="how to show the document"' in body
    assert '<div class="document-body" id="document-body"></div>' in body, "the body is the one insertion"


@requires_node
def test_the_markdown_view_is_the_page_texts_joined_by_the_projections_separator() -> None:
    """The published source, verbatim: no page numbers, no numbering of the reader's own."""
    answer = _run(
        ("documentPages", "documentMarkdown"),
        "const pages = documentPages();\n"
        "console.log(JSON.stringify({markdown: documentMarkdown(pages), pages: pages.map((p) => p.number)}));",
    )

    assert answer["pages"] == [1, 2, 3]
    assert answer["markdown"] == MARKDOWN, "byte for byte the document the corpus would publish"


@requires_node
def test_the_rendered_view_is_the_build_time_fragment_for_every_page_in_page_order() -> None:
    answer = _run(
        PANES,
        'const body = documentBody(documentPages(), "rendered");\n'
        "console.log(JSON.stringify({body}));",
    )
    rendered = answer["body"]

    assert rendered.startswith('<article class="document">')
    numbers = re.findall(r'<section class="document-page" data-page="(\d+)" data-state="complete">', rendered)
    assert numbers == ["1", "2", "3"], "every accepted page, numbered the way the demo numbers them"
    assert rendered.index("page 1") < rendered.index("page 2") < rendered.index("page 3")
    assert '<h2 class="document-page-number">Page 2</h2>' in rendered
    assert "<script" not in rendered, "the fragment is the build's, inserted as it was sanitized"


@requires_node
def test_the_toggle_switches_the_pane_between_the_rendered_document_and_its_markdown() -> None:
    """The bundle ships no parser: the rendered view is the build's fragment, the other its source."""
    answer = _run(
        DRAWING,
        "renderDocument();\n"
        "const first = state.view;\n"
        "const pressed = elements['document-views'].children.map((b) => [b.textContent, b.attrs['aria-pressed']]);\n"
        "const rendered = elements['document-body'].innerHTML;\n"
        "elements['document-views'].children[1].listeners.click();\n"
        "const second = state.view;\n"
        "const after = elements['document-body'].innerHTML;\n"
        "const pressedAfter = elements['document-views'].children"
        ".map((b) => [b.textContent, b.attrs['aria-pressed']]);\n"
        "elements['document-views'].children[0].listeners.click();\n"
        "console.log(JSON.stringify({first, pressed, rendered, second, after, pressedAfter,"
        " back: elements['document-body'].innerHTML}));",
        constants=("VIEWS",),
    )

    assert answer["first"] == "rendered", "the pane opens on the document as it would be published"
    assert answer["pressed"] == [["Rendered", "true"], ["Markdown", "false"]], (
        "one button says which view is showing"
    )
    assert 'class="document-page"' in answer["rendered"]
    assert answer["second"] == "markdown"
    assert answer["pressedAfter"] == [["Rendered", "false"], ["Markdown", "true"]]
    assert answer["after"] == '<pre class="markdown-source">' + MARKDOWN + "</pre>", (
        "the markdown view is the joined source, escaped as text, in a monospaced block"
    )
    assert answer["back"] == answer["rendered"], "the toggle comes back to the same rendered bytes"


@requires_node
def test_a_document_with_no_accepted_page_says_so_instead_of_showing_an_empty_box() -> None:
    answer = _run(
        PANES,
        'state.label = "pymupdf"; state.doc = "doc-empty";\n'
        'const body = documentBody(documentPages(), "rendered");\n'
        "console.log(JSON.stringify({body, pages: documentPages().length}));",
        label="pymupdf",
    )

    assert answer["pages"] == 0
    assert '<p class="missing">' in answer["body"]
    assert "published no page of this document" in answer["body"]
    assert "<article" not in answer["body"] and "<pre" not in answer["body"]


@requires_node
def test_a_reading_with_no_built_fragment_falls_back_to_its_markdown_in_a_sentence() -> None:
    """An older bundle carries text and chunks but no html: the pane shows the source, not nothing."""
    answer = _run(
        PANES,
        "const pages = documentPages();\n"
        "pages.forEach((page) => { delete page.reading.html; });\n"
        'const body = documentBody(pages, "rendered");\n'
        "console.log(JSON.stringify({body}));",
    )
    body = answer["body"]

    assert '<p class="missing">' in body, "the absence is said in words, in the pane's sentence style"
    assert "no rendered copy" in body
    assert '<pre class="markdown-source">' + MARKDOWN + "</pre>" in body, (
        "the document is still shown, as its source"
    )


@requires_node
def test_the_pane_follows_the_reading_selected_for_the_page_on_screen() -> None:
    """The reader's one reading toggle decides both panes: a page it did not publish is not invented."""
    answer = _run(
        PANES,
        "const answers = {};\n"
        'for (const label of ["paddleocr-vl", "pymupdf"]) {\n'
        "  state.label = label;\n"
        "  const pages = documentPages();\n"
        "  answers[label] = {numbers: pages.map((p) => p.number), text: pages.map((p) => p.reading.text)};\n"
        "}\n"
        "console.log(JSON.stringify(answers));\n",
    )

    assert answer["paddleocr-vl"]["numbers"] == [1, 2, 3]
    assert answer["pymupdf"]["numbers"] == [1, 2], "the reading that published two pages shows two"
    assert answer["pymupdf"]["text"] == [TEXTS[1], TEXTS[2]]
