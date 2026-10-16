# FinePDFs pipeline diagrams

Three Archify diagrams rendered from typed JSON specs, describing how FinePDFs (the HuggingFace
476M-document PDF pipeline) actually works.

Open any .html file in a browser: each is a self-contained page with pan and zoom, light and dark
themes, guided views, and PNG/SVG export. The .png files are static renders of the same pages.

| # | Diagram | Type | Shows |
|---|---|---|---|
| 1 | finepdfs-ingest-extract | dataflow | Steps 1-3: sourcing, screening, OCR routing, extraction |
| 2 | finepdfs-clean-publish | dataflow | Steps 4-9: cleanup, language ID, both dedup rounds, filtering, publish |
| 3 | finepdfs-implementation-ownership | architecture | Which layer implements what: third-party engines, repo code, datatrove |

## 1. Ingest, route and extract (dataflow)

Stages: Source, Screen, Route, Extract, Extracted. Two byte sources (CommonCrawl WARC, plus
re-fetching truncated PDFs over HTTP) feed one gate that splits, filters MIME and corruption, and
dedupes on PDF bytes. A trained XGBoost router then picks the cheap Docling text path or the
expensive GPU OCR path, and both converge on extracted page text.

## 2. Clean, dedupe and publish (dataflow)

Stages: Clean, Identify, Dedupe, Filter, Publish. Per-branch page rules, then GlotLID per page with
tuned thresholds, then exact dedup on normalised text, then the ModernBERT/mmBERT quality filter,
then MinHash near-duplicate removal and packaging. Two deliberate merges are visible in the diagram:
the Publish stage carries the MinHash step, and the second round of dedup is the text-level one
(the byte-level round lives in diagram 1).

## 3. Who implements what (architecture)

Three boundaries: third-party engines (pymupdf, ftfy, xgboost, a vendored Docling fork, RolmOCR),
the finepdfs repo (AGPL-3.0, five repo-owned pieces), and datatrove (executors, readers/writers,
dedup blocks, inference runner). All arrows run left to right: engines feed the repo's postprocess
step, and the repo's script and classifier drive datatrove's executor, storage and inference runner.

## Why two dataflow diagrams instead of one

A dataflow diagram is capped at five stages, and the FinePDFs pipeline has nine sequential steps.
Splitting at the extraction boundary keeps every arrow pointing left to right and avoids inventing a
fake parallel stage; a single diagram would have had to draw sequential steps as if they were
alternatives.

## Validation receipts

All three pass the showcase profile of the archify validator: 9 of 9 artifact checks, 0 composition
errors, 0 warnings.

Spec and artifact SHA-256 values:

Specs:

- finepdfs-clean-publish.dataflow.json: 50cd460f9f7a13aff43ae4483370de9d503b3849dd3b6bd45131d3d05d8d771b
- finepdfs-implementation-ownership.architecture.json: 12e454230cc60e920c50708b731d9eb9fbcca5e25431b157e5022daa21f426b6
- finepdfs-ingest-extract.dataflow.json: 944e8e4b8b5e39f649dfb00981b30fa103ba117cfc743c84e2840b5cd59d4e2e

Standalone HTML artifacts:

- finepdfs-clean-publish.dataflow.html: c233e987434d1a93c40345fa64a067a1ea99e2e4687a68062b4e76266626275f
- finepdfs-implementation-ownership.architecture.html: cfb1e9863ccedd21bd614b105456cf9a2cba91a4fb5da2b9ec67a77eb22fb268
- finepdfs-ingest-extract.dataflow.html: 2671b72356c2f182296fcfd19030770efcaaa015e090bb69f16ddac4fc69cf04

## Visual review status

Truthful status: verified by direct rendering, not by the bundled receipt tool.

The bundled command, node bin/archify.mjs visual-check, could not complete in this sandbox. Chrome
needs a writable TMPDIR and HOME (the defaults are read-only here) and container-friendly flags; after
supplying both, and after wrapping Chrome to add --no-sandbox, the harness still failed with
"Page.loadEventFired: event timed out after 15000ms" on two separate artifacts. That is a harness
limitation in this environment, not an artifact defect.

Each diagram was therefore rendered with headless Chrome directly
(google-chrome --headless=new --screenshot=...) and read back through the vision bridge. In all three
renders every stage header, node label, sublabel, tag, edge label, boundary label, legend entry and
card was present, legible and correctly placed, with left-to-right flow and no overlapping text.

## Regenerating

Run from the archify skill directory with a writable TMPDIR:

    export TMPDIR=/home/ubuntu/cocoindex/tmp
    cd /home/ubuntu/.dsh/profiles/web/node_modules/@tt-a1i/archify-dsh/skills/archify
    node bin/archify.mjs validate dataflow  SPEC.json --quality showcase --json
    node bin/archify.mjs deliver  dataflow  SPEC.json OUT.html --quality showcase --json

The specs in this folder are the frozen, validated candidates: do not hand-edit a spec after a
passing validation without re-running validate.