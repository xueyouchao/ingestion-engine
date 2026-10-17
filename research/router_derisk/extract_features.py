"""Reconstruct the documented probe measurement; keep all generated data outside git."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
from pathlib import Path

import pyarrow.parquet as pq

from schema import FEATURES

from finepdfs.infrastructure.engines.pymupdf_engine import PymupdfTextLayerProbe

REVISION = "48a1a17645b0a24460f18b77c1184a6f0e6f4a2b"
def extract(dataset: Path, output: Path) -> dict:
    """Preserve sorted shard order and in-shard row order; fail on any probe error."""
    shards = sorted((dataset / "data").glob("*.parquet"))
    if len(shards) != 4:
        raise ValueError(f"expected four data/*.parquet shards, found {len(shards)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    probe = PymupdfTextLayerProbe()
    rows = []
    shard_hashes = {}
    for shard in shards:
        with shard.open("rb") as source:
            shard_hashes[shard.name] = hashlib.file_digest(source, "sha256").hexdigest()
        parquet = pq.ParquetFile(shard)
        for batch in parquet.iter_batches(columns=["filename", "pdf", "class", "annotation"]):
            for source in batch.to_pylist():
                if source["class"] not in {"OCR", "NOCR"}:
                    raise ValueError(f"unknown class for {source['filename']}: {source['class']!r}")
                pdf = source["pdf"]
                content = pdf["bytes"]
                if not content:
                    raise ValueError(f"missing embedded PDF bytes: {source['filename']}")
                quality = probe.measure(content)
                values = {name: getattr(quality, name) for name in FEATURES}
                rows.append({
                    "filename": source["filename"],
                    "class": source["class"],
                    "annotation": source["annotation"],
                    **{name: values[name] for name in FEATURES},
                })
    if len(rows) != 1620 or sum(row["class"] == "OCR" for row in rows) != 227:
        raise ValueError("expected 1,620 rows with 227 OCR labels; refusing a different corpus")
    if len({row["filename"] for row in rows}) != len(rows):
        raise ValueError("duplicate filenames in corpus")
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    metadata = {
        "dataset": "HuggingFaceFW/ocr-annotations",
        "requested_revision": REVISION,
        "revision_note": "Use the pinned download command in the note; local shards are hashed below.",
        "shards_sha256": shard_hashes,
        "row_order": "sorted shard filenames, then original parquet row order",
        "rows": len(rows),
        "failures": 0,
        "features_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "versions": {name: importlib.metadata.version(name) for name in ("pymupdf", "pyarrow")},
    }
    output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8",
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(extract(args.dataset, args.output), indent=2))


if __name__ == "__main__":
    main()
