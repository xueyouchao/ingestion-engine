"""An engine whose identity is the deployment's, not the transport's.

The memo key is the engine identity (ADR-0005), so an engine that reports the address it happens to be
reached at makes a moving address mean "read everything again". A rented GPU is exactly that: Modal or
RunPod answers on a new host every run while the worker behind it is the same worker. This decorator
keeps the engine's name and its extraction and replaces only the detail with a stable, deployment-level
string, so the memo survives the address moving.
"""

from __future__ import annotations

from dataclasses import dataclass

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument
from finepdfs.domain.ports import OcrEngine


@dataclass(frozen=True, slots=True)
class StableIdentity:
    """Delegates extraction; reports a stable identity so a moving endpoint does not move the memo key."""

    engine: OcrEngine
    detail: str

    @property
    def identity(self) -> EngineIdentity:
        return EngineIdentity(name=self.engine.identity.name, detail=self.detail)

    def extract(self, document: Document) -> ExtractedDocument:
        return self.engine.extract(document)
