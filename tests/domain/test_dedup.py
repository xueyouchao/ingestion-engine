"""Exact and near duplicate detection: deterministic, corpus-level, pure."""

from __future__ import annotations

import pytest

from finepdfs.domain.dedup import (
    PublishedDocumentView,
    exact_duplicate_keepers,
    jaccard_estimate,
    near_duplicate_groups,
    plan_deduplication,
    similarity_fingerprint,
)

LONG_TEXT = " ".join(f"word{index}" for index in range(400))
NEARLY_THE_SAME = LONG_TEXT.replace("word7 ", "wrod7 ").replace("word200", "word201")


def test_a_fingerprint_is_stable_for_the_same_text_and_different_for_unrelated_text() -> None:
    assert similarity_fingerprint(LONG_TEXT) == similarity_fingerprint(LONG_TEXT)
    assert similarity_fingerprint(LONG_TEXT) != similarity_fingerprint(" ".join(f"other{i}" for i in range(400)))


def test_similar_texts_score_high_and_unrelated_texts_score_low() -> None:
    similar = jaccard_estimate(similarity_fingerprint(LONG_TEXT), similarity_fingerprint(NEARLY_THE_SAME))
    unrelated = jaccard_estimate(
        similarity_fingerprint(LONG_TEXT), similarity_fingerprint(" ".join(f"other{i}" for i in range(400)))
    )

    assert similar > 0.8
    assert unrelated < 0.2


def test_the_plan_reports_exact_duplicates_first() -> None:
    documents = [
        PublishedDocumentView(url="https://b/1.pdf", digest="aa", text=LONG_TEXT),
        PublishedDocumentView(url="https://a/1.pdf", digest="aa", text=LONG_TEXT),
        PublishedDocumentView(url="https://c/2.pdf", digest="bb", text=" ".join(f"unrelated{i}" for i in range(400))),
    ]

    plan = plan_deduplication(documents)

    duplicate = plan.duplicates_by_url()["https://b/1.pdf"]
    assert duplicate.kind == "exact"
    assert duplicate.duplicate_of == "https://a/1.pdf"
    assert duplicate.similarity == 1.0
    assert "https://c/2.pdf" in plan.keepers


def test_the_plan_reports_near_duplicates_that_are_not_exact() -> None:
    documents = [
        PublishedDocumentView(url="https://a/1.pdf", digest="aa", text=LONG_TEXT),
        PublishedDocumentView(url="https://b/1.pdf", digest="bb", text=NEARLY_THE_SAME),
    ]

    plan = plan_deduplication(documents)

    duplicate = plan.duplicates_by_url()["https://b/1.pdf"]
    assert duplicate.kind == "near"
    assert duplicate.duplicate_of == "https://a/1.pdf"
    assert plan.keepers == ("https://a/1.pdf",)


def test_unrelated_documents_are_all_keepers() -> None:
    documents = [
        PublishedDocumentView(url="https://a/1.pdf", digest="aa", text=LONG_TEXT),
        PublishedDocumentView(url="https://b/1.pdf", digest="bb", text=" ".join(f"other{i}" for i in range(400))),
    ]

    plan = plan_deduplication(documents)

    assert plan.duplicates == ()
    assert plan.keepers == ("https://a/1.pdf", "https://b/1.pdf")


def test_a_chain_of_similar_documents_is_one_group() -> None:
    """A~B and B~C but not A~C: dropping B must not leave C, its near-duplicate, in the corpus."""
    first = tuple([0] * 10)
    second = tuple([0] * 8 + [1, 1])
    third = tuple([2, 2] + [0] * 6 + [1, 1])
    signatures = {"https://a/1.pdf": first, "https://b/2.pdf": second, "https://c/3.pdf": third}

    groups = near_duplicate_groups(signatures, threshold=0.8)

    assert groups == [["https://a/1.pdf", "https://b/2.pdf", "https://c/3.pdf"]]


@pytest.mark.parametrize("order", [("https://a/1.pdf", "https://b/1.pdf"), ("https://b/1.pdf", "https://a/1.pdf")])
def test_the_keeper_of_a_digest_is_the_smallest_url_whatever_the_order(order) -> None:
    first, second = order

    keepers = exact_duplicate_keepers({first: "aa", second: "aa", "https://c/2.pdf": "bb"})

    assert keepers[first] == "https://a/1.pdf"
    assert keepers[second] == "https://a/1.pdf"
    assert keepers["https://c/2.pdf"] == "https://c/2.pdf"
