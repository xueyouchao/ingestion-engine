"""The committed demo corpus manifest is a real, internally consistent set of Crawl Records."""

from __future__ import annotations

from pathlib import Path

from finepdfs.infrastructure.corpus.manifest import CorpusManifest, relative_pdf_path

MANIFEST = Path(__file__).resolve().parents[2] / "docs" / "demo" / "pdf-demo-corpus-manifest.json"


def test_the_pinned_demo_manifest_names_fifteen_distinct_real_documents() -> None:
    manifest = CorpusManifest.load(MANIFEST)

    assert len(manifest.entries) == 15
    assert len(manifest.urls) == 15
    assert len(manifest.digests) == 15
    assert manifest.crawl_id == "CC-MAIN-2026-39"
    for entry in manifest.entries:
        assert entry.crawl_id == "CC-MAIN-2026-39"
        assert entry.file == relative_pdf_path(entry.digest)
        assert entry.bytes > 0 and entry.length > 0
        assert entry.warc_filename.startswith("crawl-data/CC-MAIN-2026-39/")
        assert entry.offset >= 0


def test_the_pinned_demo_manifest_keeps_the_noaa_corpus_and_the_verified_aoml_records() -> None:
    manifest = CorpusManifest.load(MANIFEST)

    assert sum(entry.url.startswith("https://www.noaa.gov/") for entry in manifest.entries) == 12
    assert sum(entry.url.startswith("https://www.aoml.noaa.gov/") for entry in manifest.entries) == 3
    assert {
        "459716fc0351dd1ab374eda66f152bc32116387e95c7a6437ad7382c6844db52",
        "b5f7ac05c298f1a0db0fff96325b0ab51fb5050de0b03f65001c2c2a3a20265f",
        "efda8b60552528173c02784b108b40b5ec113e0903ef66ff2ba2fa51ca8331a9",
    } <= manifest.digests
