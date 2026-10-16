# ADR-0003: Fetch and process are separate steps, with extraction memoized on digest and engine

**Status:** accepted

## Context

FinePDFs splits its pipeline into stages that read and write staged directories, so a re-run skips
completed work. CocoIndex gives a stronger form of the same idea: a declarative target state with
component-level and function-level memoization.

## Decision

Fetching is a CLI step that produces a corpus directory plus manifest. Processing is a CocoIndex
application whose components are keyed by document URL and whose expensive extraction is memoized on
the PDF content digest **and** engine identity. Cheap downstream logic (cleaning, language policy,
publish policy) is deliberately *not* part of the memoized unit, so changing a rule re-runs only that
rule.

## Consequences

- A rule change is seconds; an engine switch re-extracts only what that engine has not already seen.
- Removing a Crawl Record removes its declared target state, which is the deletion path.
- The memo boundary is a design decision that must be respected when adding stages: expensive work
  goes behind memoized extraction, cheap work stays outside.