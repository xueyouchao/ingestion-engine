"""An engine whose identity is the deployment's, not the transport's.

The memo key is the engine identity (ADR-0005), so an engine that reports the address it happens to be
reached at makes a moving address mean "read everything again". A rented GPU is exactly that: Modal or
RunPod answers on a new host every run while the worker behind it is the same worker. This decorator
keeps the engine's name and its extraction and replaces only the detail with a stable, deployment-level
string, so the memo survives the address moving -- and stamps the same identity on what it returns, so
the published record moves with the memo and not with the transport.
"""

from __future__ import annotations

from dataclasses import dataclass

from finepdfs.domain.document import Document, EngineIdentity, ExtractedDocument
from finepdfs.domain.ports import OcrEngine, ReadSink


@dataclass(frozen=True, slots=True)
class StableIdentity:
    """Delegates extraction; names the memo key and the record for the deployment, not the transport."""

    engine: OcrEngine
    detail: str

    @property
    def identity(self) -> EngineIdentity:
        return EngineIdentity(name=self.engine.identity.name, detail=self.detail)

    def extract(self, document: Document, sink: ReadSink | None = None) -> ExtractedDocument:
        """Extract with the wrapped engine, then stamp the result with the stable identity.

        The returned document's engine is what the published record is built from, so delegating the
        extraction unchanged would write the transport's address into every record. Rewriting only the
        engine field keeps the pages the engine produced and makes the record as stable as the memo
        key: the host can move without the record moving (ADR-0007).
        """
        extracted = self.engine.extract(document, sink)
        return ExtractedDocument(pages=extracted.pages, engine=self.identity)
