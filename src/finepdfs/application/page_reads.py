"""Reading one accepted Page Text, for a citation and for an agent's second tool.

The only source is the published document's declared page export under ``out/pages``, the same file the
browser reads. Markdown is a projection and is never parsed back into a citation, and a page export is
only current when the published record beside it names the same digest: a replacement or a removal
retracts the citation instead of leaving stale text readable.

Every outcome is a value, not an exception, because an agent has to be able to report "not found" or
"unsupported" to its model: an absent page, an excluded or duplicate Document, an unpublished page and
a page left behind by a replacement are different answers with different reasons.
"""

from __future__ import annotations

from dataclasses import dataclass

from finepdfs.domain.ports import DeclaredPageReader
from finepdfs.domain.publishing import PageRecord
from finepdfs.domain.source_identity import require_source_identity


@dataclass(frozen=True, slots=True)
class PageRead:
    """One answer to "what does this page say?": the text, or why there is no citable page."""

    doc_id: str
    page_number: int
    status: str
    reason: str
    page: PageRecord | None = None
    accepted_pages: tuple[int, ...] = ()

    @property
    def found(self) -> bool:
        return self.status == "accepted"

    @property
    def citation(self) -> str | None:
        return f"{self.doc_id} p.{self.page_number}" if self.page is not None else None

    def as_json(self) -> dict:
        payload = {
            "doc_id": self.doc_id,
            "page_number": self.page_number,
            "status": self.status,
            "found": self.found,
            "reason": self.reason,
            "citation": self.citation,
            "accepted_pages": list(self.accepted_pages),
        }
        if self.page is not None:
            payload.update(self.page.as_json())
        return payload


def read_pdf_page(reader: DeclaredPageReader, doc_id: str, page_number: int) -> PageRead:
    """Read the exact accepted Page Text of one original page, or say why it is not citable."""
    _validate(doc_id, page_number)
    if not reader.has_output():
        return PageRead(doc_id, page_number, "not-found", f"no declared output at {reader.root}")

    exclusion = reader.exclusion(doc_id)
    if exclusion is not None:
        kind, detail = exclusion
        return PageRead(
            doc_id,
            page_number,
            "unsupported",
            f"{doc_id} is unsupported evidence: excluded as {kind}: {detail}".rstrip(": "),
        )

    pages = reader.accepted_pages(doc_id)
    digest = reader.published_digest(doc_id)
    if pages is None:
        if digest is None:
            return PageRead(
                doc_id,
                page_number,
                "not-found",
                f"{doc_id} is not a published document: absent, excluded or retracted",
            )
        return PageRead(
            doc_id,
            page_number,
            "unsupported",
            f"{doc_id} has no accepted page export; its citation is not declared state",
        )
    if digest is None:
        return PageRead(
            doc_id,
            page_number,
            "unsupported",
            f"{doc_id} has no current published record; its page export is not accepted evidence",
        )

    accepted = tuple(page.page_number for page in pages)
    for page in pages:
        if page.page_number != page_number:
            continue
        if page.digest != digest:
            return PageRead(
                doc_id,
                page_number,
                "unsupported",
                (
                    f"{doc_id} page {page_number} names digest {page.digest} but the published record "
                    f"names {digest}; stale evidence is not served"
                ),
                accepted_pages=accepted,
            )
        if page.doc_id != doc_id:
            return PageRead(
                doc_id,
                page_number,
                "unsupported",
                f"the page export claims document {page.doc_id}, not {doc_id}",
                accepted_pages=accepted,
            )
        return PageRead(doc_id, page_number, "accepted", "accepted page", page, accepted)

    return PageRead(
        doc_id,
        page_number,
        "not-found",
        f"page {page_number} is not an accepted page of {doc_id} (accepted pages: {list(accepted)})",
        accepted_pages=accepted,
    )


def _validate(doc_id: str, page_number: int) -> None:
    require_source_identity(doc_id)
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError(f"page_number must be a 1-based integer, not {page_number!r}")
