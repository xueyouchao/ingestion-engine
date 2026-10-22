"""The demo's document pane: the completed pages of one job, kept as the reading reports them.

The store is fed two ways, and both are the pipeline's Page Text: at the end of a Reading from the job's
own answer, and, while a lane reports pages, from the sink's ``on_page``. Nothing here normalizes text --
ADR-0014 keeps normalization in the engine adapter, so whatever a page carries is what the pane renders.
"""

from __future__ import annotations

from finepdfs.domain.document import PageText
from finepdfs.interfaces.demo_document import DocumentStore, DocumentView


def test_a_job_completed_from_its_reading_keeps_its_pages_in_number_order() -> None:
    store = DocumentStore()
    store.begin("job-1", page_count=3)

    store.completed("job-1", (PageText(3, "third"), PageText(1, "first"), PageText(2, "second")))

    view = store.view("job-1")
    assert [page.number for page in view.pages] == [1, 2, 3]
    assert [page.text for page in view.pages] == ["first", "second", "third"]
    assert view.total == 3


def test_a_sink_reports_only_completed_pages_and_never_a_delta() -> None:
    """``on_page`` is the exported Page Text; ``on_delta`` belongs to the transcript pane (ticket 34)."""
    store = DocumentStore()
    sink = store.sink("job-2", page_count=2)

    sink.on_delta(1, "half a page, mid-stream")
    assert store.view("job-2").pages == (), "a delta is never page text"

    sink.on_page(PageText(1, "the completed first page"))
    view = store.view("job-2")

    assert [(page.number, page.text) for page in view.pages] == [(1, "the completed first page")]
    assert view.total == 2, "the pane knows how many pages are yet to come"
    assert view.completed == 1


def test_a_page_is_replaced_rather_than_duplicated_when_its_number_repeats() -> None:
    store = DocumentStore()
    store.page("job-3", PageText(1, "a first telling"))
    store.page("job-3", PageText(1, "the final telling"))

    assert [(page.number, page.text) for page in store.view("job-3").pages] == [(1, "the final telling")]


def test_a_document_that_does_not_exist_is_an_empty_view_not_a_guess() -> None:
    view = DocumentStore().view("never-issued")

    assert view == DocumentView()
    assert view.pages == ()
    assert view.total is None


def test_the_store_forgets_the_oldest_document_beyond_its_bound() -> None:
    """Finished jobs and their pages leave together; a store that grew forever would be a leak."""
    store = DocumentStore(retained=2)
    for number in (1, 2, 3):
        store.begin(f"job-{number}")
        store.page(f"job-{number}", PageText(1, "page"))

    assert store.view("job-1").pages == (), "the oldest document was forgotten"
    assert store.view("job-2").pages and store.view("job-3").pages