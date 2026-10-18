"""The committed demo corpus manifest is a real, internally consistent set of Crawl Records."""

from __future__ import annotations

from pathlib import Path

from finepdfs.infrastructure.corpus.manifest import CorpusManifest, relative_pdf_path
from finepdfs.interfaces import demo

# One canonical location: the manifest is package data, not a copy under docs/.
MANIFEST = Path(__file__).resolve().parents[2] / "src" / "finepdfs" / "data" / demo.MANIFEST_RESOURCE

# Two real Crawl Records whose WARC payloads are byte-identical: one content digest, two URLs.
EXACT_DUPLICATE_URLS = {
    "https://www.buoybay.noaa.gov/sites/default/files/files/s_geography_text.pdf",
    "https://content.buoybay.noaa.gov/sites/default/files/files/s_geography_text.pdf",
}
EXACT_DUPLICATE_DIGEST = "5e4c1b36e9949a148616b4e199fdd12a9fc70643c76f607625da376352d25595"

# Two real Crawl Records of the same seasonal summary, differing in their running headers. Measured
# near-duplicate similarity is recorded in docs/demo/pdf-demo-corpus.md; it needs the PDFs, so an
# offline test can only pin their identity and their distinct digests.
NEAR_DUPLICATE_URLS = {
    "https://buoybay.noaa.gov/sites/default/files/2024-04/NCBOWinter202324SeasonalSummary.pdf",
    "https://buoybay.noaa.gov/sites/default/files/2024-05/NCBOWinter202324SeasonalSummaryA.pdf",
}


def test_the_pinned_demo_manifest_names_nineteen_real_crawl_records() -> None:
    manifest = CorpusManifest.load(MANIFEST)

    assert len(manifest.entries) == 19
    assert len(manifest.urls) == 19
    assert len(manifest.digests) == 18, "the exact-duplicate pair shares one content digest"
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


def test_the_pinned_demo_manifest_carries_a_real_exact_duplicate_pair() -> None:
    manifest = CorpusManifest.load(MANIFEST)

    pair = [entry for entry in manifest.entries if entry.digest == EXACT_DUPLICATE_DIGEST]
    assert {entry.url for entry in pair} == EXACT_DUPLICATE_URLS
    assert len({entry.file for entry in pair}) == 1, "identical bytes are stored once"
    assert len({entry.warc_filename for entry in pair}) == 2, "two distinct real captures, not one record"


def test_the_pinned_demo_manifest_carries_a_real_near_duplicate_pair() -> None:
    manifest = CorpusManifest.load(MANIFEST)

    pair = [entry for entry in manifest.entries if entry.url in NEAR_DUPLICATE_URLS]
    assert {entry.url for entry in pair} == NEAR_DUPLICATE_URLS
    assert len({entry.digest for entry in pair}) == 2, "near duplicates are different bytes, not exact copies"
