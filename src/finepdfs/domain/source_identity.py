"""Source Identity: where a Document came from, named by its source URL and never by its bytes.

The glossary's Source Identity is the first 16 hex digits of the SHA-256 of the source URL: it is the
``doc_id`` of the Document's page and chunk records, and the key its declared outputs are named after.
Replacing the bytes changes the Document and its digest but not its Source Identity, so the same exports
are rewritten in place; removing the Crawl Record retracts them.

One module owns both the derivation and the shape. The page reader, the published output layout and the
explorer's path validation all read a ``doc_id``, and three copies of "what a doc_id looks like" were
three chances to disagree about which strings may name a declared file.
"""

from __future__ import annotations

import hashlib
import re

SOURCE_IDENTITY_DIGITS = r"[0-9a-f]{16}"
SOURCE_IDENTITY_PATTERN = rf"^{SOURCE_IDENTITY_DIGITS}$"

_SOURCE_IDENTITY = re.compile(SOURCE_IDENTITY_PATTERN)


def source_identity(url: str) -> str:
    """The Source Identity of one source URL: the first 16 hex digits of the URL's SHA-256.

    The digest is of the URL, not of the bytes, so the same exports are rewritten in place when the
    Document at that origin is replaced.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def is_source_identity(value: object) -> bool:
    """Whether a value has the shape of a Source Identity, so it may name declared outputs.

    The shape is what keeps a URL-derived key from traversing a path: a ``doc_id`` is 16 lowercase hex
    digits and nothing else, so ``../records`` cannot be mistaken for one.
    """
    return isinstance(value, str) and _SOURCE_IDENTITY.fullmatch(value) is not None


def require_source_identity(value: object) -> str:
    """The value as a Source Identity, or a ValueError naming what was expected instead."""
    if not is_source_identity(value):
        raise ValueError(f"doc_id must be a 16-character source identity, not {value!r}")
    return value
