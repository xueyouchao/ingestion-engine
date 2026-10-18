"""The document key names every output file, so it has to be stable, documented and tested."""

from __future__ import annotations

import hashlib
import pathlib

from finepdfs.domain.source_identity import source_identity
from finepdfs.infrastructure.corpus.index import CorpusIndex, document_key
from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path

ONE = "https://a.example/one.pdf"
TWO = "https://b.example/two.pdf"


def test_a_key_is_the_first_16_hex_digits_of_the_url_sha256() -> None:
    """A known-good literal: `sha256sum` of the URL, cut to its first 16 digits, not recomputed here."""
    assert document_key(ONE) == "925214985eaf79ec"
    assert document_key(ONE) == source_identity(ONE), "the output key and a citation's doc_id are one value"


def test_two_urls_get_two_keys() -> None:
    assert document_key(ONE) != document_key(TWO)


def test_documents_come_out_sorted_by_url_whatever_the_manifest_order(tmp_path) -> None:
    corpus_dir = tmp_path / "corpus"
    entries = [
        CorpusEntry(
            url=url,
            digest=hashlib.sha256(url.encode("utf-8")).hexdigest(),
            file=relative_pdf_path(hashlib.sha256(url.encode("utf-8")).hexdigest()),
            bytes=10,
            crawl_id="CC-MAIN-2026-39",
            warc_filename="crawl-data/CC-MAIN-2026-39/segments/x/warc/y.warc.gz",
            offset=1,
            length=10,
        )
        for url in (TWO, ONE)
    ]
    CorpusManifest(crawl_id="CC-MAIN-2026-39", domain="a.example", entries=entries).save(
        corpus_dir / "manifest.json"
    )

    documents = CorpusIndex(corpus_dir).documents()

    assert [document.url for document in documents] == [ONE, TWO]
    assert [document.key for document in documents] == [document_key(ONE), document_key(TWO)]
    assert all(pathlib.Path(document.path).is_absolute() for document in documents)
