"""The demo's document fragment: Page Text rendered to HTML by the pinned, allowlisted pair.

ADR-0014 makes the rendered pane a server-side answer built from Page Text, and the page inserts exactly
one fragment at one call site. This module is that renderer and nothing else -- no HTTP, no job state:

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
- The page numbers come from the pages the job has and the count it knows, never from a guess: the
  ordering rule is ``domain.publishing.pages_in_order``, and the last page is the job's count when it has
  one and the highest page number it produced otherwise, so a document without a count claims no page the
  read never reported. After a failed read the pages it never reached are ``data-state="unread"`` and say
  they were not read, rather than promising a skeleton that will never be replaced.

Both dependencies are pinned in the optional ``[demo]`` extra (``pyproject.toml`` and
``docs/demo/requirements-demo.txt``) and imported lazily by ``_dependencies`` -- never when this module
is imported, so the base install stays free of them and the server can start without either. When they
are absent, ``render_document`` raises ``RenderingNotInstalled`` and the server answers in words instead
of showing unescaped text.
"""

from __future__ import annotations

from finepdfs.domain.publishing import pages_in_order
from finepdfs.interfaces.demo_job import DocumentView

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
UNREAD_CLASS = "unread"


class RenderingNotInstalled(RuntimeError):
    """The pinned renderer/sanitizer pair is absent, so the demo has no honest fragment to serve."""


def render_document(view: DocumentView) -> str:
    """The document pane's fragment for one job's pages, inert by construction."""
    try:
        markdown_it, nh3 = _dependencies()
    except ImportError as error:  # the extra is optional; falling back to text would be a lie
        raise RenderingNotInstalled(NOT_INSTALLED) from error
    renderer = markdown_it("commonmark", {"html": False, "linkify": False, "typographer": False})
    renderer.enable("table")
    pages = pages_in_order(view.pages)
    completed = {page.number: page for page in pages}
    # The count is the job's when it knows one, and the highest page number it has otherwise: a page the
    # read has not reported is only drawn when routing already said it exists.
    last = view.total if view.total is not None else (pages[-1].number if pages else 0)

    attributes = f' data-pages="{view.total}"' if view.total is not None else ""
    parts = [f'<article class="{DOCUMENT_CLASS}"{attributes} data-completed="{len(completed)}">']
    if last == 0:
        parts.append(_empty(view.failed))
    for number in range(1, last + 1):
        page = completed.get(number)
        if page is not None:
            body = _sanitize(nh3, renderer.render(page.text))
            parts.append(_complete(number, body))
        elif view.failed:
            parts.append(_unread(number))
        else:
            parts.append(_pending(number))
    parts.append("</article>")
    return "\n".join(parts)


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


def _unread(number: int) -> str:
    """One page the failed read never reached: named as not read, never as a skeleton still to come."""
    label = f'<h2 class="document-page-number">Page {number}</h2>'
    words = f'<p class="{UNREAD_CLASS}" data-state="unread">Page {number} was not read: the read failed.</p>'
    return (
        f'<section class="{PAGE_CLASS}" data-page="{number}" data-state="unread">\n'
        f"{label}\n{words}\n</section>"
    )


def _empty(failed: bool) -> str:
    """What an empty document says: that nothing is read yet, or that the failed read read nothing."""
    if failed:
        return '<p class="document-empty" data-state="unread">No page was read: the read failed.</p>'
    return '<p class="document-empty" data-state="pending">No page has completed yet.</p>'


def _dependencies():
    """Import the pinned pair on demand: the one seam a test can remove to prove the honest absence.

    This is deliberately not a module-level import. ``finepdfs.interfaces.demo_server`` imports this
    module so the pane has a renderer to ask for, and a base install must still start and answer the
    rest of the demo without either dependency present.
    """
    import nh3
    from markdown_it import MarkdownIt

    return MarkdownIt, nh3