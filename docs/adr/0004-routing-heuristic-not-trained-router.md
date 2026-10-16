# ADR-0004: OCR routing is an explicit heuristic; the trained router is not reproduced

**Status:** accepted

## Context

FinePDFs routes with an XGBoost model trained on 1,350 hand-annotated PDFs, and filters with
ModernBERT/mmBERT quality heads. Those annotations and the production training pipeline are not part
of this workspace, and a hand-annotated set cannot be re-derived here.

## Decision

Routing is a documented heuristic over the same *kind* of features (characters per page, fraction of
pages with text, garbled-character ratio, image-dominated pages), behind an OcrRoutingPolicy object.
The decision, the features and the thresholds are recorded per document so the rule is auditable and
tunable. A trained model can replace the policy without touching the pipeline. No quality-classifier
port is introduced until there is a model to put behind it.

## Consequences

- Routing quality is not comparable to FinePDFs; the mechanism and the interface are.
- Every routing decision is explainable from the output record, which the trained model also would not
  give for free.
- Replacing the policy with a model is a contained change.
- Language identification is a separate seam and is not decided here; see ADR-0001.