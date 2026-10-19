"""Export a static reader bundle from runs' declared outputs.

The bundle is a **derived artifact**, never declared state. It reads `run-summary.json`, `records/`,
`pages/`, `chunks/` and the pinned corpus, and writes one directory a browser can open straight from
`file://`:

    index.html                    the page and the manifest (no fetch: everything but images is inlined)
    images/<digest>-p<page>.jpg   page renders, shared by every state -- an image is a render of the PDF,
                                  not of the engine -- loaded per page, so first paint is one page
    bundle.json                   the machine-readable contract: schema version, states, fingerprints

The images stay separate on purpose: inlining them makes one 28 MiB file, which a browser preview pane
or a mail attachment will not take. `--inline` does it anyway for a single-file copy.

It never curates, deduplicates, routes or publishes, it never writes into a state directory, and it does
not carry a timing sidecar: the page shows declared facts only.

    PYTHONPATH=src python -m finepdfs.interfaces.cli bundle --out bundle \
        --state gpu=.demo-gpu/out --state pymupdf=.demo/out --corpus .demo-gpu/corpus
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymupdf

from finepdfs.interfaces.bundle_page import BUNDLE_HTML

BUNDLE_VERSION = 1
DEFAULT_DPI = 72
PAGE_QUALITY = 60
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
IMAGES = "images"
INDEX_NAME = "index.html"
CONTRACT_NAME = "bundle.json"
SUMMARY_NAME = "run-summary.json"


class BundleError(RuntimeError):
    """Raised when the bundle cannot be built from what the runs declared."""


@dataclass(frozen=True)
class StateRun:
    """One completed run's declared outputs, as the bundle reads them."""

    label: str
    out: Path
    summary: dict[str, Any]
    records: dict[str, dict[str, Any]]
    pages: tuple[dict[str, Any], ...]
    chunks: tuple[dict[str, Any], ...]
    excluded: tuple[dict[str, Any], ...]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BundleError(f"cannot read {path}: {error}") from error


def _read_jsonl(directory: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as error:
                raise BundleError(f"{path}:{number} is not JSON: {error}") from error
    return tuple(rows)


def read_state(label: str, out: Path | str) -> StateRun:
    """Read one completed run. A state without a summary is refused, never guessed at."""
    root = Path(out)
    summary_path = root / SUMMARY_NAME
    if not summary_path.is_file():
        raise BundleError(f"{root} declares no {SUMMARY_NAME}; the bundle reads completed runs only")
    summary = _read_json(summary_path)
    records: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "records").glob("*.json")):
        record = _read_json(path)
        if "digest" in record:
            records[record["digest"]] = record
    pages = _read_jsonl(root / "pages")
    chunks = _read_jsonl(root / "chunks")
    excluded = tuple(
        _read_json(path) for path in sorted((root / "excluded").glob("*.json"))
    )
    if not records or not pages:
        raise BundleError(f"{root} declares no records or no pages; there is nothing to bundle")
    return StateRun(
        label=label, out=root, summary=summary, records=records, pages=pages, chunks=chunks, excluded=excluded
    )


def declared_fingerprint(run: StateRun) -> str:
    """What the bundle mirrors: the declared summary and the sorted chunk files, like the index does."""
    digest = hashlib.sha256()
    digest.update((run.out / SUMMARY_NAME).read_bytes())
    for path in sorted((run.out / "chunks").glob("*.jsonl")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _state_block(run: StateRun) -> dict[str, Any]:
    routing = run.summary.get("routing", {})
    published = run.summary.get("published", {})
    exclusions = run.summary.get("exclusions", {})
    return {
        "label": run.label,
        "engine": run.summary.get("engine", {}).get("identity", ""),
        "documents": published.get("documents"),
        "pages": published.get("page_records"),
        "native": routing.get("native"),
        "ocr": routing.get("ocr"),
        "exclusions": exclusions.get("total"),
        "chunks": len(run.chunks),
        "declared_fingerprint": declared_fingerprint(run),
        "path": str(run.out),
    }


def _page_key(doc_id: str, page_number: int) -> str:
    return f"{doc_id}:{page_number}"


def _words(page: pymupdf.Page, dpi: int) -> list[list[Any]] | None:
    """The engine-independent word boxes, mapped to the rendered image; None when the page has none."""
    scale = dpi / 72.0
    boxes: list[list[Any]] = []
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        rect = pymupdf.Rect(x0, y0, x1, y1) * page.rotation_matrix
        boxes.append([round(rect.x0 * scale, 1), round(rect.y0 * scale, 1), round(rect.x1 * scale, 1),
                      round(rect.y1 * scale, 1), word])
    return boxes or None


def _render(
    out: Path, corpus: Path, digest: str, page_number: int, dpi: int, quality: int, *, inline: bool
) -> dict[str, Any]:
    """Render one page once.

    Separate files are the default: a page plus an `images/` directory travels together, loads one page
    at a time, and stays small enough to preview. `inline` bakes each render into the page as a data URI
    for a single-file copy -- at the cost of one large file.
    """
    source = corpus / "pdfs" / f"{digest}.pdf"
    if not source.is_file():
        raise BundleError(f"the corpus has no {source.name}; restore the corpus before bundling")
    with pymupdf.open(source) as document:
        if page_number > document.page_count:
            raise BundleError(f"{source.name} has no page {page_number}")
        page = document.load_page(page_number - 1)
        pixmap = page.get_pixmap(dpi=dpi)
        jpeg = pixmap.tobytes("jpeg", jpg_quality=quality)
        boxes = _words(page, dpi)
        width, height = pixmap.width, pixmap.height
    if inline:
        image = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    else:
        image = f"{IMAGES}/{digest}-p{page_number}.jpg"
        target = out / image
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(jpeg)
    return {"image": image, "w": width, "h": height, "words": boxes, "bytes": len(jpeg)}


def build_bundle(
    out: Path | str,
    runs: list[StateRun],
    corpus: Path | str,
    dpi: int = DEFAULT_DPI,
    max_bytes: int = DEFAULT_MAX_BYTES,
    quality: int = PAGE_QUALITY,
    inline: bool = False,
    link: str | None = None,
) -> dict[str, Any]:
    """Write the bundle and return its contract. Refuses rather than shrinks when over budget."""
    if not runs:
        raise BundleError("the bundle needs at least one state")
    target = Path(out)
    target.mkdir(parents=True, exist_ok=True)
    corpus_root = Path(corpus)
    manifest: dict[str, Any] = {
        "schema_version": BUNDLE_VERSION,
        "dpi": dpi,
        "default_state": runs[0].label,
        "link": link,
        "states": [_state_block(run) for run in runs],
        "documents": {},
        "pages": {},
        "excluded": {},
    }

    for run in runs:
        manifest["excluded"][run.label] = [
            {"url": row.get("url"), "kind": row.get("kind"), "reason": row.get("reason")}
            for row in run.excluded
        ]

    readings: dict[tuple[str, int], dict[str, Any]] = {}
    pages_by_document: dict[str, set[int]] = {}
    for run in runs:
        for page in run.pages:
            doc_id = page["doc_id"]
            digest = page["digest"]
            document = manifest["documents"].setdefault(
                doc_id, {"url": page["source_url"], "digest": digest, "reads": {}}
            )
            record = run.records.get(digest, {})
            document["reads"][run.label] = {
                "engine": page.get("engine", ""),
                "routing": record.get("routing_decision", ""),
                "reason": record.get("routing_reason", ""),
                "chars": record.get("character_count"),
                "pages": record.get("page_count"),
            }
            pages_by_document.setdefault(doc_id, set()).add(page["page_number"])
            entry = readings.setdefault((doc_id, page["page_number"]), {"readings": {}})
            entry["readings"][run.label] = {"text": page["text"], "chunks": []}

    for run in runs:
        for chunk in run.chunks:
            key = (chunk["doc_id"], chunk["page_number"])
            reading = readings.get(key, {}).get("readings", {}).get(run.label)
            if reading is not None:
                reading["chunks"].append({
                    "id": chunk["chunk_id"],
                    "start": chunk["start_offset"],
                    "end": chunk["end_offset"],
                    "ordinal": chunk["ordinal"],
                })

    needed = sorted({(page["digest"], page["page_number"]) for run in runs for page in run.pages})
    if inline and (target / IMAGES).is_dir():
        for stale in (target / IMAGES).iterdir():
            stale.unlink()
        (target / IMAGES).rmdir()

    rendered: dict[tuple[str, int], dict[str, Any]] = {}
    for digest, page_number in needed:
        rendered[(digest, page_number)] = _render(
            target, corpus_root, digest, page_number, dpi, quality, inline=inline
        )

    for (doc_id, page_number), entry in sorted(readings.items()):
        for reading in entry["readings"].values():
            reading["chunks"].sort(key=lambda chunk: chunk["start"])
        manifest["pages"][_page_key(doc_id, page_number)] = {
            "readings": entry["readings"],
            **{k: v for k, v in rendered[(manifest["documents"][doc_id]["digest"], page_number)].items()
               if k != "bytes"},
        }

    for doc_id, numbers in pages_by_document.items():
        manifest["documents"][doc_id]["pages"] = len(numbers)

    payload = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = BUNDLE_HTML.replace("__DATA__", payload)
    (target / INDEX_NAME).write_text(html, encoding="utf-8")

    image_bytes = sum(item["bytes"] for item in rendered.values())
    # Inline images live inside the html already, so the page's own bytes are the whole weight.
    total = len(html.encode("utf-8")) if inline else image_bytes + len(html.encode("utf-8"))
    contract = {
        "schema_version": BUNDLE_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dpi": dpi,
        "mode": "inline" if inline else "split",
        "link": link,
        "default_state": runs[0].label,
        "states": manifest["states"],
        "documents": len(manifest["documents"]),
        "pages": len(manifest["pages"]),
        "chunks": len({
            chunk["id"]
            for entry in readings.values()
            for reading in entry["readings"].values()
            for chunk in reading["chunks"]
        }),
        "images": [] if inline else [{"path": item["image"], "bytes": item["bytes"]} for item in sorted(
            rendered.values(), key=lambda item: item["image"])],
        "image_bytes": image_bytes,
        "total_bytes": total,
        "max_bytes": max_bytes,
    }
    if total > max_bytes:
        raise BundleError(
            f"the bundle weighs {total / 1024 / 1024:.1f} MiB, over the {max_bytes / 1024 / 1024:.0f} MiB "
            "ceiling; lower --dpi or --max-bytes rather than shipping a silent shrink"
        )
    (target / CONTRACT_NAME).write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    return contract


def check_bundle(out: Path | str, runs: list[StateRun]) -> list[str]:
    """Labels whose declared evidence the bundle no longer mirrors; empty means fresh."""
    contract = _read_json(Path(out) / CONTRACT_NAME)
    recorded = {state["label"]: state["declared_fingerprint"] for state in contract.get("states", [])}
    return [run.label for run in runs if recorded.get(run.label) != declared_fingerprint(run)]
