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

*Amended in ticket 08.* This record owns the language seam too, because it is the record of how a
model-backed component sits behind a port and how its weights reach the host. `LanguageIdentifier`
ships two adapters: `ScriptLanguageIdentifier`, which needs nothing installed and is the default, and
`FastTextLanguageIdentifier`, which names the language rather than the script with a downloaded
`lid.176` model. The model sits behind its own small protocol so the adapter's page logic is tested
without it, it is reached as a downloaded file (`FINEPDFS_LANGUAGE_MODEL`) behind an optional extra, and
the two-letter labels it emits are normalised to ISO 639-3 (`en` -> `eng`) at the adapter. The port
promises a language *or* script tag, not one code system: the script adapter answers ISO 15924 codes
(`Latn`, `Grek`, `Cyrl`), which name a script and not a language, and `FINEPDFS_LANGUAGE` decides which
of the two a record carries. The mode is deliberately not part of the extraction memo key stated above:
it changes no extracted text, only the recorded tag, and language identification runs in the deciding
pass, which is not memoised, so the next run re-curates and rewrites the record. This reverses the
earlier position that no model-backed language adapter ships: this environment can run the model, so
shipping it is a feature rather than a claim. That reversal belongs here, not in the record about OCR
routing (ADR-0004), which decides only how a page is routed.

## Consequences

- The pipeline host needs no GPU and no multi-gigabyte weights.
- Two engines that need a live endpoint cannot be tested in CI; their protocol mapping is tested from
  recorded responses instead.
- Adding an engine means one adapter module plus one registry entry; the CLI's list of engines is
  `KNOWN_ENGINES` from that registry, so the two cannot drift apart.
- Language identification shows the pattern generalises: a model-free default, an opt-in model-backed
  adapter, and a model reached by download rather than vendored, so the base install carries no weights.