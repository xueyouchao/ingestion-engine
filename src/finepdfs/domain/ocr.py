"""Deciding whether a document needs real OCR. Pure policy."""

from __future__ import annotations

from dataclasses import dataclass, field

from finepdfs.domain.document import TextLayerQuality


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The routing outcome, with the features behind it so the decision can be audited."""

    needs_ocr: bool
    reason: str
    features: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OcrRoutingPolicy:
    """When a text layer is good enough to trust.

    These are heuristic thresholds, not a trained model: see ADR-0004. They are deliberately
    conservative, because routing a scanned page to the text layer produces an empty document.
    """

    min_characters_per_page: float = 200.0
    min_text_coverage: float = 0.5
    max_garbled_ratio: float = 0.05
    max_image_dominated_share: float = 0.5

    def decide(self, quality: TextLayerQuality) -> RoutingDecision:
        features = {
            "page_count": float(quality.page_count),
            "characters_per_page": quality.characters_per_page,
            "text_coverage": quality.text_coverage,
            "garbled_ratio": quality.garbled_ratio,
            "image_dominated_share": quality.image_dominated_share,
        }

        if quality.page_count == 0:
            return RoutingDecision(True, "the PDF reports no pages", features)
        if quality.text_coverage < self.min_text_coverage:
            return RoutingDecision(
                True,
                f"only {quality.text_coverage:.0%} of pages carry text, below the {self.min_text_coverage:.0%} floor",
                features,
            )
        if quality.characters_per_page < self.min_characters_per_page:
            return RoutingDecision(
                True,
                f"{quality.characters_per_page:.0f} characters per page is below the "
                f"{self.min_characters_per_page:.0f} floor",
                features,
            )
        if quality.garbled_ratio > self.max_garbled_ratio:
            return RoutingDecision(
                True,
                f"{quality.garbled_ratio:.1%} of characters are garbled, above the "
                f"{self.max_garbled_ratio:.0%} limit",
                features,
            )
        if quality.image_dominated_share > self.max_image_dominated_share:
            return RoutingDecision(
                True,
                f"{quality.image_dominated_share:.0%} of pages are images with little text, above the "
                f"{self.max_image_dominated_share:.0%} limit",
                features,
            )
        return RoutingDecision(
            False,
            f"the text layer is healthy: {quality.characters_per_page:.0f} characters per page across "
            f"{quality.text_coverage:.0%} of pages",
            features,
        )
