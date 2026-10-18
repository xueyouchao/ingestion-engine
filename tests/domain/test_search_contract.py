"""The search contract the browser and the agent both read: one hit is one Chunk plus its ranking.

A hit may not carry its own copy of the chunk's citation fields. The Chunk is the one source of truth
for what a citation names, so the JSON a view reads is the chunk's own payload with the snippet and the
score added, and a change to a citation field cannot reach one reader and not the other.
"""

from __future__ import annotations

from finepdfs.domain.chunks import Chunk
from finepdfs.domain.search import SearchHit

URL = "https://a.example/one.pdf"
DIGEST = "1" * 64


def _chunk() -> Chunk:
    return Chunk(
        chunk_id="c0ffee",
        doc_id="925214985eaf79ec",
        source_url=URL,
        digest=DIGEST,
        engine="pymupdf@1.28.2",
        language="eng",
        chunking_version="chunking-v1",
        ordinal=4,
        page_number=3,
        start_offset=10,
        end_offset=24,
        text="Brighton buoy",
    )


def test_a_search_hit_is_one_chunk_plus_its_snippet_and_score() -> None:
    chunk = _chunk()

    hit = SearchHit(chunk=chunk, snippet="<mark>Brighton</mark> buoy", score=2.5)

    assert hit.chunk is chunk
    assert hit.as_json() == {
        "doc_id": "925214985eaf79ec",
        "chunk_id": "c0ffee",
        "source_url": URL,
        "digest": DIGEST,
        "engine": "pymupdf@1.28.2",
        "language": "eng",
        "chunking_version": "chunking-v1",
        "ordinal": 4,
        "page_number": 3,
        "start_offset": 10,
        "end_offset": 24,
        "text": "Brighton buoy",
        "snippet": "<mark>Brighton</mark> buoy",
        "score": 2.5,
    }
