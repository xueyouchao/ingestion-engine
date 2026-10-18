# ADR-0002: The corpus is real Common Crawl PDFs; synthetic PDFs are deleted

**Status:** accepted

## Context

The earlier benchmark generated synthetic PDFs and downloaded a handful of arXiv papers. Synthetic
files prove nothing about extraction quality, and the source of truth for this pipeline shape is
Common Crawl, which is what FinePDFs actually processes.

## Decision

The only corpus is real PDFs fetched from Common Crawl. The index supplies a WARC file name, byte
offset and length; fetching issues a single HTTP range request and parses one WARC record. Fetching is
a separate, resumable step that writes PDF files named by content digest plus a manifest. The
generated PDFs, their outputs and the generator are removed from the repository.

## Consequences

- Every claim about extraction is made against real crawled documents.
- Processing never depends on the network: the corpus is on disk.
- The corpus is not committed; it is reproducible from the manifest and the fetch command. The
  pinned demo manifest is committed as package data (`src/finepdfs/data/`), so `finepdfs-demo
  restore` replays it from a wheel install as well as from a source checkout.
- Common Crawl licence and attribution apply to the corpus.