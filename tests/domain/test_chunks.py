"""Deterministic chunks from accepted Page Texts: page-local, offset-true and versioned.

The search index is only as trustworthy as this step: a chunk cites a page, and its offsets slice the
exact accepted text, so a reader can be sent back to the page a hit came from. These tests are pure;
the flow's declaration of the exports is tested through a real run in
``tests/infrastructure/test_search_index.py``.
"""

from __future__ import annotations

import itertools
from dataclasses import replace

from finepdfs.domain.chunks import CHUNKING_VERSION, chunk_page_records
from finepdfs.domain.publishing import PageRecord

DOC_ID = "0123456789abcdef"
URL = "https://example.org/doc.pdf"
DIGEST = "a" * 64
ENGINE = "pymupdf@controlled"


def _record(page_number: int, text: str) -> PageRecord:
    return PageRecord(
        doc_id=DOC_ID,
        source_url=URL,
        digest=DIGEST,
        page_number=page_number,
        text=text,
        engine=ENGINE,
    )


def _chunks(records, **kwargs):
    return chunk_page_records(records, language="Latn", **kwargs)


def test_a_short_page_is_one_chunk_whose_offsets_slice_its_text() -> None:
    text = "Alpha paragraph.\n\nBeta paragraph."

    chunks = _chunks((_record(7, text),))

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.page_number == 7
    assert chunk.ordinal == 0
    assert chunk.text == text
    assert text[chunk.start_offset : chunk.end_offset] == chunk.text
    assert (chunk.start_offset, chunk.end_offset) == (0, len(text))


def test_chunks_follow_the_original_page_numbers_and_ordinals() -> None:
    chunks = _chunks((_record(2, "second accepted page"), _record(4, "fourth accepted page")))

    assert [chunk.page_number for chunk in chunks] == [2, 4]
    assert [chunk.ordinal for chunk in chunks] == [0, 1]
    assert [chunk.text for chunk in chunks] == ["second accepted page", "fourth accepted page"]


def test_every_chunk_carries_the_source_identity_and_engine() -> None:
    chunks = _chunks((_record(1, "alpha beta"),))

    for chunk in chunks:
        assert chunk.doc_id == DOC_ID
        assert chunk.source_url == URL
        assert chunk.digest == DIGEST
        assert chunk.engine == ENGINE
        assert chunk.language == "Latn"
        assert chunk.chunking_version == CHUNKING_VERSION


def test_a_large_page_is_split_at_paragraph_boundaries_without_losing_text() -> None:
    paragraphs = [f"Paragraph {index} " + "word " * 30 for index in range(1, 9)]
    text = "\n\n".join(paragraphs)

    chunks = _chunks((_record(1, text),), max_characters=250)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 250 for chunk in chunks)
    assert " ".join(chunk.text for chunk in chunks).split() == text.split(), "word order survives, nothing is lost"
    for previous, following in itertools.pairwise(chunks):
        assert previous.end_offset < following.start_offset
    assert all(chunk.text == chunk.text.strip() for chunk in chunks)


def test_a_single_paragraph_longer_than_the_limit_is_split_at_whitespace() -> None:
    text = "word " * 400

    chunks = _chunks((_record(3, text),), max_characters=100)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 100 for chunk in chunks)
    assert all(chunk.text == chunk.text.strip() for chunk in chunks), "no chunk starts or ends in whitespace"
    assert all(text[chunk.start_offset : chunk.end_offset] == chunk.text for chunk in chunks)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    covered = {index for chunk in chunks for index in range(chunk.start_offset, chunk.end_offset)}
    assert {index for index, character in enumerate(text) if not character.isspace()} <= covered, (
        "every non-whitespace character of the page is inside exactly one chunk"
    )
    assert len(covered) == sum(chunk.end_offset - chunk.start_offset for chunk in chunks), "no chunk overlaps"


def test_a_page_with_no_text_produces_no_chunks() -> None:
    assert _chunks((_record(1, ""), _record(2, " \n\t\n ")),) == ()


def test_chunk_ids_are_deterministic_and_bound_to_their_evidence() -> None:
    base = _chunks((_record(1, "alpha beta"),))
    again = _chunks((_record(1, "alpha beta"),))
    assert base[0].chunk_id == again[0].chunk_id, "the same evidence always produces the same id"
    assert len(base[0].chunk_id) == 32

    assert base[0].chunk_id != _chunks((_record(1, "alpha beta"),), version="chunking-v2")[0].chunk_id
    assert base[0].chunk_id != _chunks((_record(2, "alpha beta"),))[0].chunk_id
    assert base[0].chunk_id != _chunks((replace(_record(1, "alpha beta"), digest="b" * 64),))[0].chunk_id
    assert base[0].chunk_id != _chunks((replace(_record(1, "alpha beta"), doc_id="fedcba9876543210"),))[0].chunk_id


def test_offsets_are_part_of_the_identity_not_only_the_text() -> None:
    page = "one two three four five six"

    chunks = _chunks((_record(1, page),), max_characters=9)

    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == len(set(ids)), "two chunks of one page never share an id"
    assert all(chunk.text in page for chunk in chunks)


def test_the_chunking_version_is_reported_for_every_chunk() -> None:
    default = _chunks((_record(1, "alpha"),))
    bumped = _chunks((_record(1, "alpha"),), version="chunking-v2")

    assert default[0].chunking_version == CHUNKING_VERSION
    assert bumped[0].chunking_version == "chunking-v2"


def test_the_declared_fingerprint_names_every_stored_field_not_only_id_and_text() -> None:
    """A relabel is new declared evidence even when its text and chunk identity are unchanged.

    The fingerprint is what makes a stale index visible (ADR-0008). Language and engine are filters
    a reader can apply, so changing either changes the evidence the index mirrors even though the
    chunk it names is the same slice of the same page.
    """
    from finepdfs.domain.chunks import chunk_set_fingerprint

    chunk = _chunks((_record(1, "alpha beta"),))[0]
    base = chunk_set_fingerprint((chunk,))
    assert base == chunk_set_fingerprint((chunk,)), "the same evidence fingerprints the same"

    relabelled = replace(chunk, language="eng")
    assert relabelled.chunk_id == chunk.chunk_id, "a label is not part of the chunk identity"
    assert chunk_set_fingerprint((relabelled,)) != base, "a language change must move the fingerprint"

    for changed in (
        replace(chunk, engine="docling@cli"),
        replace(chunk, text="alpha gamma"),
        replace(chunk, doc_id="fedcba9876543210"),
        replace(chunk, source_url="https://example.org/other.pdf"),
        replace(chunk, digest="b" * 64),
        replace(chunk, chunking_version="chunking-v2"),
        replace(chunk, ordinal=1),
        replace(chunk, page_number=2),
        replace(chunk, start_offset=1),
        replace(chunk, end_offset=len(chunk.text) - 1),
    ):
        assert chunk_set_fingerprint((changed,)) != base, f"{changed} must move the fingerprint"
