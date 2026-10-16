"""The document key names every output file, so it has to be stable, documented and tested."""

from __future__ import annotations

import hashlib
import pathlib

from finepdfs.infrastructure.corpus.index import CorpusIndex, document_key
from finepdfs.infrastructure.corpus.manifest import CorpusEntry, CorpusManifest, relative_pdf_path

ONE = "https://a.example/one.pdf"
TWO = "https://b.example/two.pdf"


def test_a_key_is_the_first_16_hex_digits_of_the_url_sha256() -> None:
    key = document_key(ONE)

    assert key == hashlib.sha256(ONE.encode("utf-8")).hexdigest()[:16]
    assert len(key) == 16
    assert all(character in "0123456789abcdef" for character in key)


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
