# ADR-0001: OCR engines are ports; heavy models are reached over one HTTP transport

**Status:** accepted

## Context

The pipeline must run on a CPU-only box today and hand work to PaddleOCR-VL or MinerU on a GPU host
tomorrow, and the next best document model should be an afternoon of work, not a refactor.

## Decision

Extraction is expressed as a port (OcrEngine). Four adapters ship: pymupdf (native text layer),
docling (local CLI), paddleocr-vl (local CLI or document VLM) and mineru (local CLI or service).
PaddleOCR-VL speaks OpenAI-compatible chat completions; MinerU's service answers a multipart
`POST /file_parse` with a content list. Two wire protocols, so each has its own client, and each
adapter is a recipe of prompt, image encoding and response parsing. Every engine that runs a command is
handed one subprocess runner carrying the configured timeout, so a hung tool is bounded exactly as a
hung request is. Engine selection is configuration, and engine identity is part of the memo key, so
switching engines re-extracts and changing a downstream rule does not.

## Consequences

- The pipeline host needs no GPU and no multi-gigabyte weights.
- Two engines that need a live endpoint cannot be tested in CI; their protocol mapping is tested from
  recorded responses instead.
- Adding an engine means one adapter module plus one registry entry; the CLI's list of engines is
  `KNOWN_ENGINES` from that registry, so the two cannot drift apart.