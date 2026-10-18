"""Source Identity: the URL-derived name a Document's declared outputs are addressed by.

The glossary's Source Identity is the first 16 hex digits of the SHA-256 of the source URL, never of the
bytes. One domain module owns both the derivation and the shape, because the page reader, the published
output layout and the explorer's path validation all have to agree on what a doc_id is.
"""

from __future__ import annotations

import pytest

from finepdfs.domain.source_identity import (
    is_source_identity,
    require_source_identity,
    source_identity,
)

URL = "https://a.example/one.pdf"


def test_the_source_identity_is_the_first_16_hex_digits_of_the_url_digest() -> None:
    """A known-good literal: `sha256sum` of the URL, cut to its first 16 digits, not recomputed here."""
    assert source_identity(URL) == "925214985eaf79ec"


def test_a_source_identity_is_16_lowercase_hex_digits() -> None:
    assert is_source_identity("0123456789abcdef")
    assert is_source_identity("0" * 16)
    for value in ("", "0123456789abcde", "0" * 17, "A" * 16, "../../records", "records/x.json", None, 12):
        assert not is_source_identity(value), value


def test_requiring_a_source_identity_names_what_was_expected() -> None:
    """The error says which shape was wanted, so a rejected path is not mistaken for a missing file."""
    with pytest.raises(ValueError, match="16-character source identity"):
        require_source_identity("../records")

    assert require_source_identity("0" * 16) == "0" * 16
