# 11. Export the demo reader bundle as a derived artifact

Date: 2026-10-19

## Status

Accepted. Budget call for the demo, in the shape of ADR-0008 and ADR-0009.

## Context

A stranger has to be able to open one link and see what the pipeline did to a PDF: the page image beside
the engine's reading, the routing decision, the Chunks, and a search over the corpus. The declared target
state is markdown, records, pages, chunks and exclusions; none of it is a page somebody can open, and
`explore` needs a Python process per visitor.

Three shapes were available:

1. Make the page a **declared output** of the flow. A presentation change would re-run the flow, page
   renders would enter CocoIndex memoisation, and every citation would depend on a view.
2. Serve `explore` publicly. That needs a live server, and it turns a demo of the outputs into a
   deployment of the pipeline.
3. **Derive** a static bundle from the declared outputs in an interface command, and serve that.

## Decision

`finepdfs bundle` writes `index.html`, `bundle.json` and `images/` from a completed run's declared files
and the corpus PDFs. It never writes into a state.

- Page images are keyed by PDF digest and page number, so two engine readings share one render: a render
  is of the PDF, not of the engine. 313 renders serve both states.
- `bundle.json` records the schema version, the states, the counts and, per state, a
  `declared_fingerprint` -- a sha256 over the run summary and the sorted chunk files -- so `--check` can
  say whether the bundle still mirrors what the flow declared.
- `--inline` bakes the same renders into the page as data URIs, for a single self-contained file. The
  default writes siblings, because a 3.84 MiB page plus 18.19 MiB of images is what a static host wants
  to serve and what a browser can open without a server.
- The 32 MiB ceiling refuses rather than shrinking silently: a bundle that quietly drops pages is a lie
  about coverage.
- The bundle is gitignored (`/dist/`) and rebuilt, never committed, so derived evidence cannot drift in
  git.

## Consequences

+ A demo is a copy of declared evidence: every fact on the page traces to a run summary, a record, a page
  file or a chunk file, and `--check` fails when they drift.
+ The page is client-side only -- corpus search, page navigation and both readings run in the browser --
  so a static file host is enough. Here an `nginx` container serves `dist/bundle` behind the existing
  Traefik (`deploy/bundle/`).
- The bundle is a second copy of page renders on disk (~22 MiB for this corpus), regenerated per build.
- Only pages of published documents appear; exclusions are shown as counts with their verbatim reasons,
  not as pages.
