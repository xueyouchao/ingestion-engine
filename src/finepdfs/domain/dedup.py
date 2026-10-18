"""Exact and near-duplicate detection. Pure and deterministic."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from finepdfs.domain.publishing import CurationOutcome

DuplicateKind = Literal["exact", "near"]

_SIGNATURE_SEED = b"finepdfs-minhash-v1"

@dataclass(frozen=True, slots=True)
class DuplicateDecision:
    """Why a document is not published, and which document it duplicates."""

    url: str
    kind: DuplicateKind
    duplicate_of: str
    similarity: float


@dataclass(frozen=True, slots=True)
class DedupPlan:
    """Who is published, and who is a duplicate of whom."""

    keepers: tuple[str, ...]
    duplicates: tuple[DuplicateDecision, ...]

    def duplicates_by_url(self) -> dict[str, DuplicateDecision]:
        return {decision.url: decision for decision in self.duplicates}


def exact_duplicate_keepers(digests_by_url: Mapping[str, str]) -> dict[str, str]:
    """For every URL, the URL that should represent its content.

    The keeper is the smallest URL sharing a digest, so the choice does not depend on the order the
    corpus was read.
    """
    keepers_by_digest: dict[str, str] = {}
    for url, digest in digests_by_url.items():
        current = keepers_by_digest.get(digest)
        if current is None or url < current:
            keepers_by_digest[digest] = url
    return {url: keepers_by_digest[digest] for url, digest in digests_by_url.items()}


def shingle_hashes(text: str, shingle_size: int = 5) -> list[int]:
    """Hash every word n-gram of a text. Short texts fall back to their words."""
    words = text.split()
    if len(words) < shingle_size:
        pieces: Iterable[str] = words or [text.strip()]
    else:
        pieces = (" ".join(words[i : i + shingle_size]) for i in range(len(words) - shingle_size + 1))
    return [int.from_bytes(hashlib.blake2b(piece.encode("utf-8"), digest_size=8).digest(), "big") for piece in pieces]


def similarity_fingerprint(text: str, permutations: int = 64, shingle_size: int = 5) -> tuple[int, ...]:
    """A MinHash signature: the smallest hash under each of a fixed set of permutations."""
    hashes = shingle_hashes(text, shingle_size)
    if not hashes:
        return tuple([0] * permutations)
    signature = []
    for index in range(permutations):
        salt = hashlib.blake2b(_SIGNATURE_SEED + str(index).encode("ascii"), digest_size=8).digest()
        signature.append(min(h ^ int.from_bytes(salt, "big") for h in hashes))
    return tuple(signature)


def jaccard_estimate(left: Sequence[int], right: Sequence[int]) -> float:
    """Share of signature positions that agree -- an estimate of the Jaccard similarity."""
    if not left or not right:
        return 0.0
    agreements = sum(1 for a, b in zip(left, right, strict=True) if a == b)
    return agreements / len(left)


def near_duplicate_groups(
    signatures_by_url: Mapping[str, Sequence[int]],
    threshold: float = 0.8,
) -> list[list[str]]:
    """Group URLs whose fingerprints are at least this similar.

    A candidate is compared against every member of a group rather than only its first, and a candidate
    that is close to two groups joins them. Comparing against the leader alone lets a chain leak: with
    A~B and B~C but not A~C, dropping B as a duplicate of A would leave C in the corpus even though it
    duplicates the document that was just removed (ADR-0006).
    """
    groups: list[list[str]] = []
    for url in sorted(signatures_by_url):
        signature = signatures_by_url[url]
        joined = [
            group
            for group in groups
            if any(jaccard_estimate(signature, signatures_by_url[member]) >= threshold for member in group)
        ]
        if not joined:
            groups.append([url])
            continue
        merged = [url]
        for group in joined:
            merged.extend(group)
            groups.remove(group)
        groups.append(sorted(merged))
    return [group for group in groups if len(group) > 1]


@dataclass(frozen=True, slots=True)
class PublishedDocumentView:
    """A published document as the dedup pass sees it: identity, content and text."""

    url: str
    digest: str
    text: str


def plan_deduplication(
    documents: Sequence[PublishedDocumentView],
    similarity_threshold: float = 0.8,
) -> DedupPlan:
    """Decide the keepers of a corpus: exact duplicates first, then near duplicates."""
    digests_by_url = {document.url: document.digest for document in documents}
    keeper_by_url = exact_duplicate_keepers(digests_by_url)

    duplicates = [
        DuplicateDecision(url=url, kind="exact", duplicate_of=keeper, similarity=1.0)
        for url, keeper in sorted(keeper_by_url.items())
        if keeper != url
    ]

    keepers = [document for document in documents if keeper_by_url[document.url] == document.url]
    signatures = {document.url: similarity_fingerprint(document.text) for document in keepers}
    for group in near_duplicate_groups(signatures, similarity_threshold):
        leader = min(group)
        for url in sorted(group):
            if url == leader:
                continue
            duplicates.append(
                DuplicateDecision(
                    url=url,
                    kind="near",
                    duplicate_of=leader,
                    similarity=round(jaccard_estimate(signatures[url], signatures[leader]), 4),
                )
            )
    duplicated = {decision.url for decision in duplicates}
    return DedupPlan(
        keepers=tuple(url for url in sorted(keeper_by_url) if url not in duplicated),
        duplicates=tuple(sorted(duplicates, key=lambda decision: decision.url)),
    )


@dataclass(frozen=True, slots=True)
class InheritedFate:
    """The disposition an exact duplicate inherits when its digest keeper was never published.

    It is not a duplicate of a published Document, so it is recorded with the keeper's own fate -- the
    Publish Policy's disposition and reason -- rather than being pointed at a keeper that does not exist
    (ADR-0006).
    """

    url: str
    kind: str
    keeper: str
    reason: str


@dataclass(frozen=True, slots=True)
class Deduplication:
    """The corpus-wide duplicate decision: who is published, who duplicates whom, and each fate."""

    plan: DedupPlan
    inherited: tuple[InheritedFate, ...] = ()

    def duplicates_by_url(self) -> dict[str, DuplicateDecision]:
        return self.plan.duplicates_by_url()

    def inherited_by_url(self) -> dict[str, InheritedFate]:
        return {fate.url: fate for fate in self.inherited}


def decide_duplicates(
    digests_by_url: Mapping[str, str],
    outcomes: Mapping[str, CurationOutcome],
    similarity_threshold: float = 0.8,
) -> Deduplication:
    """Decide both kinds of duplicate for the whole corpus, in one pass over values.

    This is the only place the decision is made, so the flow's declared exclusions and the `dry-run`
    cannot disagree about what a duplicate duplicates or what kind it is. It reads what curation
    produced and nothing else: an exact duplicate costs no Extraction, because it is decided from the
    digest it shares rather than from its own text.

    ``digests_by_url`` covers every Document in the corpus, including the exact duplicates that were
    never read; ``outcomes`` holds what curating a Document produced. An exact duplicate follows its
    digest keeper's near decision when the keeper is itself a near-duplicate loser, and inherits the
    keeper's disposition when the keeper was dropped or failed, because in neither case did the run
    publish a keeper it could honestly name (ADR-0006).
    """
    plan = plan_deduplication(
        [
            PublishedDocumentView(url=url, digest=digest, text=outcomes[url].markdown)
            for url, digest in digests_by_url.items()
            if url in outcomes and outcomes[url].published
        ],
        similarity_threshold=similarity_threshold,
    )
    near_by_url = {decision.url: decision for decision in plan.duplicates if decision.kind == "near"}
    exact: list[DuplicateDecision] = []
    inherited: list[InheritedFate] = []
    keeper_by_url = exact_duplicate_keepers(digests_by_url)
    for url in sorted(digests_by_url):
        keeper = keeper_by_url[url]
        if keeper == url:
            continue
        near = near_by_url.get(keeper)
        if near is not None:
            exact.append(
                DuplicateDecision(
                    url=url, kind="near", duplicate_of=near.duplicate_of, similarity=near.similarity
                )
            )
            continue
        keeper_outcome = outcomes.get(keeper)
        if keeper_outcome is None or not keeper_outcome.published:
            kind = keeper_outcome.status if keeper_outcome is not None else "failed"
            reason = (
                keeper_outcome.reason
                if keeper_outcome is not None
                else "it was not read and declared nothing"
            )
            inherited.append(InheritedFate(url=url, kind=kind, keeper=keeper, reason=reason))
            continue
        exact.append(DuplicateDecision(url=url, kind="exact", duplicate_of=keeper, similarity=1.0))
    return Deduplication(
        plan=DedupPlan(
            keepers=plan.keepers,
            duplicates=tuple(sorted([*plan.duplicates, *exact], key=lambda decision: decision.url)),
        ),
        inherited=tuple(inherited),
    )
