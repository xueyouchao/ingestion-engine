"""The demo's document fragment: Page Text rendered to HTML by the pinned, allowlisted pair.

ADR-0014 makes the rendered pane a server-side answer built from Page Text, and the page inserts exactly
one fragment at one call site. This module is that renderer and nothing else -- no HTTP, no store:

- ``markdown-it-py`` renders the Page Text as Markdown (``commonmark``, raw HTML off, tables on).
- ``nh3`` sanitizes the result against a structural allowlist: headings, paragraphs, lists, emphasis,
  code blocks, blockquotes, horizontal rules and tables, with ``colspan``/``rowspan`` the only data an
  element may carry. No ``script``, ``style``, ``iframe`` or ``img``; no ``on*`` attribute; no link and
  so no ``javascript:`` target -- model output must never create navigation. Math stays text.
- The pane adds its own trusted wrapper: an ``<article class="document">`` holding one
  ``<section class="document-page" data-page="K" data-state="complete">`` per completed page, in
  numeric order with ``page K`` as the separator, and a ``data-state="pending"`` section with a
  ``.skeleton`` paragraph for a page that has not completed. A page number with no completed Page Text
  is never rendered from anything else, so a half-parsed table cannot appear.

Both dependencies are pinned in the optional ``[demo]`` extra (``pyproject.toml`` and
``docs/demo/requirements-demo.txt``) and imported lazily by ``_dependencies`` -- never when this module
is imported, so the base install stays free of them and the server can start without either. When they
are absent, ``render_document`` raises ``RenderingNotInstalled`` and the server answers in words instead
of showing unescaped text.
"""

from __future__ import annotations

from finepdfs.interfaces.demo_document import DocumentView

# What the answer says when the extra is not installed: the same sentence the server serves, so the
# endpoint and the renderer cannot describe the absence differently.
NOT_INSTALLED = (
    "rendering is not installed: the demo's document pane needs the [demo] extra "
    "(markdown-it-py==4.2.0 and nh3==0.3.7); install it with `pip install -e '.[demo]'`. "
    "The document's markdown is still available at the .md answer."
)

# The structure a page may carry. ``a`` is absent deliberately: the first cut has no navigation, so a
# ``javascript:`` target has no element to attach to. ``img`` is out of scope (ADR-0014 open items).
ALLOWED_TAGS = frozenset(
    {
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
    }
)

# The only attributes allowed, and only on the two cells a merged table uses. Everything else -- an
# event handler, a style, an id, a URL -- is removed by the sanitizer rather than filtered by hand.
ALLOWED_ATTRIBUTES = {
    "th": frozenset({"colspan", "rowspan"}),
    "td": frozenset({"colspan", "rowspan"}),
}

DOCUMENT_CLASS = "document"
PAGE_CLASS = "document-page"
SKELETON_CLASS = "skeleton"


class RenderingNotInstalled(RuntimeError):
    """The pinned renderer/sanitizer pair is absent, so the demo has no honest fragment to serve."""


def rendering_installed() -> bool:
    """Whether the ``[demo]`` extra's pair can be imported right now."""
    try:
        _dependencies()
    except ImportError:
        return False
    return True


def render_document(view: DocumentView) -> str:
    """The document pane's fragment for one job's completed pages, inert by construction."""
    try:
        markdown_it, nh3 = _dependencies()
    except ImportError as error:  # the extra is optional; falling back to text would be a lie
        raise RenderingNotInstalled(NOT_INSTALLED) from error
    renderer = markdown_it("commonmark", {"html": False, "linkify": False, "typographer": False})
    renderer.enable("table")
    completed = {page.number: page for page in view.pages}
    last = max(view.total if view.total is not None else 0, max(completed, default=0))
    if view.total is None and completed:
        # The job has not learned its page count, but the lane completes pages in page-number order:
        # the next number is the page being read now, and no later one can be claimed.
        last = max(last, max(completed) + 1)

    attributes = f' data-pages="{view.total}"' if view.total is not None else ""
    parts = [f'<article class="{DOCUMENT_CLASS}"{attributes} data-completed="{len(completed)}">']
    if last == 0:
        parts.append('<p class="document-empty" data-state="pending">No page has completed yet.</p>')
    for number in range(1, last + 1):
        page = completed.get(number)
        if page is None:
            parts.append(_pending(number))
        else:
            body = _sanitize(nh3, renderer.render(page.text))
            parts.append(_complete(number, body))
    parts.append("</article>")
    return "\n".join(parts)


def sanitize_fragment(fragment: str) -> str:
    """Sanitize an HTML fragment against the structural allowlist; the renderer's own safety net."""
    try:
        _, nh3 = _dependencies()
    except ImportError as error:
        raise RenderingNotInstalled(NOT_INSTALLED) from error
    return _sanitize(nh3, fragment)


def _sanitize(nh3, fragment: str) -> str:
    """Apply the one allowlist: no tag outside it, no attribute but a table cell's span."""
    return nh3.clean(
        fragment,
        tags=set(ALLOWED_TAGS),
        attributes={tag: set(names) for tag, names in ALLOWED_ATTRIBUTES.items()},
        url_schemes=set(),
        link_rel=None,
    )


def _complete(number: int, body: str) -> str:
    label = f'<h2 class="document-page-number">Page {number}</h2>'
    return (
        f'<section class="{PAGE_CLASS}" data-page="{number}" data-state="complete">\n'
        f"{label}\n{body}\n</section>"
    )


def _pending(number: int) -> str:
    label = f'<h2 class="document-page-number">Page {number}</h2>'
    skeleton = (
        f'<p class="{SKELETON_CLASS}" data-state="pending">Page {number} is still being read.</p>'
    )
    return (
        f'<section class="{PAGE_CLASS}" data-page="{number}" data-state="pending">\n'
        f"{label}\n{skeleton}\n</section>"
    )


def _dependencies():
    """Import the pinned pair on demand: the one seam a test can remove to prove the honest absence.

    This is deliberately not a module-level import. ``finepdfs.interfaces.demo_server`` imports this
    module so the pane has a renderer to ask for, and a base install must still start and answer the
    rest of the demo without either dependency present.
    """
    import nh3
    from markdown_it import MarkdownIt

    return MarkdownIt, nh3