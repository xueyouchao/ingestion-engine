"""Exact and near-duplicate detection. Pure and deterministic."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping, Sequence

DuplicateKind = Literal["exact", "near"]

_SIGNATURE_SEED = b"finepdfs-minhash-v1"
_WORD = None


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
    agreements = sum(1 for a, b in zip(left, right) if a == b)
    return agreements / len(left)


def near_duplicate_groups(
    signatures_by_url: Mapping[str, Sequence[int]],
    threshold: float = 0.8,
) -> list[list[str]]:
    """Group URLs whose fingerprints are at least this similar, comparing in URL order."""
    groups: list[tuple[str, list[str]]] = []
    for url in sorted(signatures_by_url):
        signature = signatures_by_url[url]
        for leader, members in groups:
            if jaccard_estimate(signature, signatures_by_url[leader]) >= threshold:
                members.append(url)
                break
        else:
            groups.append((url, [url]))
    return [members for _, members in groups if len(members) > 1]


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
    text_by_url = {document.url: document.text for document in keepers}
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
