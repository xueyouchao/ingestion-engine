"""Export a static reader bundle from runs' declared outputs.

The bundle is a **derived artifact**, never declared state. It reads `run-summary.json`, `records/`,
`pages/`, `chunks/` and the pinned corpus, and writes one directory a browser can open straight from
`file://`:

    index.html                            the page and the manifest (everything but images is inlined)
    images/<digest>-p<page>-<hash>.jpg    page renders, shared by every state -- an image is a render of
                                          the PDF, not of the engine -- loaded per page, so first paint is
                                          one page. The hash is of the render's own bytes, so rebuilding
                                          with another dpi or quality produces new URLs instead of
                                          repainting a URL the host told browsers to keep for a year.
    bundle.json                           the machine-readable contract: schema version, states, fingerprint

The images stay separate on purpose: inlining them makes one 28 MiB file, which a browser preview pane
or a mail attachment will not take. `--inline` does it anyway for a single-file copy.

It never curates, deduplicates, routes or publishes, it never writes into a state directory, and it does
not carry a timing sidecar: the page shows declared facts only. It also writes nothing into the served
directory until every render succeeded and the budget check passed, so a refused rebuild leaves the
previous bundle serving.

    PYTHONPATH=src python -m finepdfs.interfaces.cli bundle --out bundle \
        --state gpu=.demo-gpu/out --state pymupdf=.demo/out --corpus .demo-gpu/corpus
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
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
# Every declared directory whose files the page copies a fact from, so the fingerprint covers them.
DECLARED_GLOBS = ("records/*.json", "pages/*.jsonl", "chunks/*.jsonl", "excluded/*.json")
# What this exporter names its renders: only files matching it are ever deleted by a rebuild.
# Both shapes this exporter has written: `<digest>-p<n>-<hash>.jpg`, and the older `<digest>-p<n>.jpg`
# that a rebuild has to be able to retire.
_IMAGE_NAME = re.compile(r"^[0-9a-f]{64}-p\d+(-[0-9a-f]{12})?\.jpg$")


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
    """Read one completed run. A state without a summary or records is refused, never guessed at.

    A state that published **no page** is not refused here: one engine can exclude every document, and
    reading it beside the engine that published is exactly what the comparison is for. `build_bundle`
    refuses when no supplied state publishes anything.
    """
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
    if not records:
        raise BundleError(f"{root} declares no records; there is nothing to bundle")
    return StateRun(
        label=label, out=root, summary=summary, records=records, pages=pages, chunks=chunks, excluded=excluded
    )


def declared_fingerprint(run: StateRun) -> str:
    """Every declared file the bundle copies: summary, records, pages, chunks and exclusions.

    The page shows each of them -- a record's routing reason, a page's text, an exclusion's verbatim
    reason -- so a fingerprint over only the summary and the chunks would call a rewritten exclusion
    reason, or another page text, fresh evidence.
    """
    digest = hashlib.sha256()
    digest.update(SUMMARY_NAME.encode("utf-8"))
    digest.update((run.out / SUMMARY_NAME).read_bytes())
    for pattern in DECLARED_GLOBS:
        for path in sorted(run.out.glob(pattern)):
            digest.update(str(path.relative_to(run.out)).encode())
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


def _render_page(corpus: Path, digest: str, page_number: int, dpi: int, quality: int) -> dict[str, Any]:
    """Render one page once, in memory.

    In memory on purpose: the served directory is only touched after every render succeeded and the
    budget check passed, so a refused or failed rebuild leaves the previous bundle serving visitors.
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
    return {"jpeg": jpeg, "w": width, "h": height, "words": boxes}


def _image_name(digest: str, page_number: int, jpeg: bytes) -> str:
    """A render's file name carries the hash of its own bytes.

    The page is served with immutable caching, so a name that outlived its bytes would pin a returning
    visitor to the old picture for a year after a `--dpi` change.
    """
    return f"{digest}-p{page_number}-{hashlib.sha256(jpeg).hexdigest()[:12]}.jpg"


def _utf16_offset(text: str, index: int) -> int:
    """The same offset counted in UTF-16 code units, which is what a JavaScript string index counts."""
    return len(text[:index].encode("utf-16-le")) // 2


def _prune_images(directory: Path, keep: set[str]) -> None:
    """Delete renders neither this build nor the one it replaced references.

    The previous generation is kept deliberately: a reader that has not reloaded still holds the old
    manifest and asks for those images only when a page is opened, so deleting them on the first build
    would break every open tab. The next build retires them.
    """
    for path in directory.iterdir():
        if path.is_file() and path.name not in keep and _IMAGE_NAME.match(path.name):
            path.unlink()


def _refuse_what_cannot_be_honest(runs: list[StateRun]) -> None:
    """Labels identify readings, and a page image must belong to the PDF its reading came from."""
    labels = [run.label for run in runs]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise BundleError(
            f"two states share the label {', '.join(duplicates)}; readings are keyed by label, so their "
            "pages, chunks and exclusions would silently merge -- give each state its own label"
        )
    if not any(run.pages for run in runs):
        raise BundleError("no supplied state publishes a page; there is nothing to show")


def _refuse_conflicting_digests(seen: dict[str, dict[str, set[str]]]) -> None:
    conflicts = {doc: digests for doc, digests in seen.items() if len(digests) > 1}
    if not conflicts:
        return
    detail = "; ".join(
        f"{doc}: " + ", ".join(
            f"{digest[:12]}… read by {' and '.join(sorted(labels))}" for digest, labels in sorted(digests.items())
        )
        for doc, digests in sorted(conflicts.items())
    )
    raise BundleError(
        "these states read different PDFs for the same source URL, so one page image cannot be honest for "
        f"both ({detail}); bundle one state at a time"
    )


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
    _refuse_what_cannot_be_honest(runs)
    target = Path(out)
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
    page_digests: dict[tuple[str, int], str] = {}
    digests_by_document: dict[str, dict[str, set[str]]] = {}
    for run in runs:
        for page in run.pages:
            doc_id = page["doc_id"]
            digest = page["digest"]
            document = manifest["documents"].setdefault(
                doc_id, {"url": page["source_url"], "digest": digest, "reads": {}}
            )
            digests_by_document.setdefault(doc_id, {}).setdefault(digest, set()).add(run.label)
            page_digests[(doc_id, page["page_number"])] = digest
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

    _refuse_conflicting_digests(digests_by_document)

    for run in runs:
        for chunk in run.chunks:
            key = (chunk["doc_id"], chunk["page_number"])
            reading = readings.get(key, {}).get("readings", {}).get(run.label)
            if reading is None:
                continue
            # The declared offsets count Python characters; the page slices JavaScript strings, so they
            # are converted here, once, rather than every reader having to remember.
            reading["chunks"].append({
                "id": chunk["chunk_id"],
                "start": _utf16_offset(reading["text"], chunk["start_offset"]),
                "end": _utf16_offset(reading["text"], chunk["end_offset"]),
                "ordinal": chunk["ordinal"],
            })

    # Keyed by PDF and page: two source URLs can name the same PDF, and one render serves both.
    renders: dict[tuple[str, int], dict[str, Any]] = {}
    for key in sorted(readings):
        digest = page_digests[key]
        if (digest, key[1]) not in renders:
            renders[(digest, key[1])] = _render_page(corpus_root, digest, key[1], dpi, quality)

    for (digest, page_number), item in renders.items():
        item["image"] = (
            "data:image/jpeg;base64," + base64.b64encode(item["jpeg"]).decode("ascii")
            if inline
            else f"{IMAGES}/{_image_name(digest, page_number, item['jpeg'])}"
        )
        item["bytes"] = len(item["jpeg"])

    for (doc_id, page_number), entry in sorted(readings.items()):
        for reading in entry["readings"].values():
            reading["chunks"].sort(key=lambda chunk: chunk["start"])
        item = renders[(page_digests[(doc_id, page_number)], page_number)]
        manifest["pages"][_page_key(doc_id, page_number)] = {
            "readings": entry["readings"],
            "image": item["image"],
            "w": item["w"],
            "h": item["h"],
            "words": item["words"],
        }

    for doc_id, numbers in pages_by_document.items():
        manifest["documents"][doc_id]["pages"] = len(numbers)

    payload = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = BUNDLE_HTML.replace("__DATA__", payload)

    image_bytes = sum(item["bytes"] for item in renders.values())
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
            renders.values(), key=lambda item: item["image"])],
        "image_bytes": image_bytes,
        "total_bytes": total,
        "max_bytes": max_bytes,
    }
    if total > max_bytes:
        raise BundleError(
            f"the bundle weighs {total / 1024 / 1024:.1f} MiB, over the {max_bytes / 1024 / 1024:.0f} MiB "
            "ceiling; lower --dpi or --max-bytes rather than shipping a silent shrink"
        )

    previous = _previous_image_names(target)
    # Nothing above this line wrote to the served directory: a refused rebuild leaves the old bundle up.
    target.mkdir(parents=True, exist_ok=True)
    images = target / IMAGES
    if inline:
        if images.is_dir():
            shutil.rmtree(images)
    else:
        images.mkdir(exist_ok=True)
        for item in renders.values():
            (target / item["image"]).write_bytes(item["jpeg"])
    (target / INDEX_NAME).write_text(html, encoding="utf-8")
    if not inline:
        _prune_images(images, {Path(item["image"]).name for item in renders.values()} | previous)
    (target / CONTRACT_NAME).write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    return contract


def _previous_image_names(target: Path) -> set[str]:
    """The renders the contract now on disk references, if it can be read."""
    contract_path = target / CONTRACT_NAME
    if not contract_path.is_file():
        return set()
    try:
        contract = _read_json(contract_path)
    except BundleError:
        return set()
    return {
        Path(entry["path"]).name
        for entry in contract.get("images", [])
        if isinstance(entry, dict) and entry.get("path")
    }


def check_bundle(out: Path | str, runs: list[StateRun]) -> list[str]:
    """Labels whose declared evidence the bundle no longer mirrors; empty means fresh."""
    contract = _read_json(Path(out) / CONTRACT_NAME)
    if contract.get("schema_version") != BUNDLE_VERSION:
        raise BundleError(
            f"{Path(out) / CONTRACT_NAME} is schema version {contract.get('schema_version')!r}, not "
            f"{BUNDLE_VERSION}; rebuild the bundle before checking it"
        )
    recorded = {state["label"]: state["declared_fingerprint"] for state in contract.get("states", [])}
    return [run.label for run in runs if recorded.get(run.label) != declared_fingerprint(run)]
