# ADR-0001: OCR engines are ports; heavy models are reached over one HTTP transport

**Status:** accepted

## Context

The pipeline must run on a CPU-only box today and hand work to PaddleOCR-VL or MinerU on a GPU host
tomorrow, and the next best document model should be an afternoon of work, not a refactor.

## Decision

Extraction is expressed as a port (OcrEngine). Four adapters ship: pymupdf (native text layer),
docling (local CLI), paddleocr-vl and mineru (document VLMs). The two VLMs are reached through a
single OpenAI-compatible chat-completions transport, so retries, timeouts and image encoding live in
one place and each engine adapter is only a recipe: prompt, image encoding, response parsing. Engine
selection is configuration, and engine identity is part of the memo key, so switching engines
re-extracts and changing a downstream rule does not.

## Consequences

- The pipeline host needs no GPU and no multi-gigabyte weights.
- Two engines that need a live endpoint cannot be tested in CI; their protocol mapping is tested from
  recorded responses instead.
- Adding an engine means one adapter module plus one registry entry.