# Agent evidence: real tool calls over accepted pages

This records what was observed, not what is assumed. It answers one question for the demo: can a real
tool-capable model call the two agent tools over the same accepted evidence the browser reads, and
return an answer whose every citation names a page the tools actually read? The short answer is **yes**:
with a stronger model and the citation contract enforced in code, all five answerable questions cite
the correct accepted page and the unanswerable control returns `INSUFFICIENT EVIDENCE` with no
citation.

Observed on 2026-10-18 on the rehearsal host. Machine state changes; the commands are the check, not
the answers in this file.

## What changed since the first run

The first evidence run used the local `qwen3:1.7b` and exposed three defects that the loop reported but
did not correct: Q1 cited `p.10` while the sentence is on `p.8`, Q3 appended a contradictory
`INSUFFICIENT EVIDENCE` sentence after a supported answer, and Q4 wrote "page 37" in prose with no
structured citation. Those were model-quality findings and they are fixed at the contract level now,
not by editing the answers:

* a final answer may cite **only** a page `read_pdf_page` returned as accepted; a citation for any
  other page is removed from the answer text;
* a page number written in prose ("page 37") is not a citation;
* when the answer has no verified citation, or carries a citation for a page nobody read, the loop
  takes **one** bounded repair turn asking the model to read the page that supports the claim; if that
  does not yield a verified citation, the answer becomes an explicit insufficient-evidence reply;
* a contradictory `INSUFFICIENT EVIDENCE` tail after a supported answer is dropped;
* an insufficient-evidence reply carries no citations at all;
* the hard tool-call budget and the printed/JSON tool trace are unchanged.

The tests that pin this are `tests/application/test_agent.py` (strip an unread page, repair a prose-only
reference, drop a contradictory tail, fall back to insufficient evidence) and the CLI `ask` tests in
`tests/interfaces/test_cli.py`.

## Environment check

```bash
curl -sS --max-time 5 'http://[::1]:11434/api/tags'
curl -sS --max-time 4 'http://127.0.0.1:11434/api/tags'
```

* The IPv6 endpoint `http://[::1]:11434` answered. `qwen3:1.7b` lists capabilities `completion`,
  `tools`, `thinking`; the cloud-tagged models `deepseek-v4.1-flash:cloud` and
  `deepseek-v4-pro:cloud` list `tools` as well.
* The IPv4 endpoint `127.0.0.1:11434` timed out, so the demo uses the IPv6 URL.
* A model list is **not** evidence of tool support. The next check was an actual inference with a tool
  schema through the repository's stdlib client
  (`finepdfs/infrastructure/llm/chat_completions.py`) at `http://[::1]:11434/v1/chat/completions`.

## Capability probe: real `tool_calls`, not a model name

A prompt that required a synthetic `lookup_page` call returned a real `tool_calls` array:

```json
deepseek-v4.1-flash:cloud  0.49 s
{"role": "assistant", "content": "I'll look up that page for you.",
 "tool_calls": [{"id": "call_6gx1yqi8", "type": "function",
                 "function": {"name": "lookup_page",
                              "arguments": "{\"doc_id\":\"9bf665589f7333a8\",\"page_number\":8}"}}]}

deepseek-v4-pro:cloud      0.68 s
{"role": "assistant", "content": "",
 "tool_calls": [{"id": "call_2ti23a5i", ... same schema-valid arguments ...}]}
```

`deepseek-v4.1-flash:cloud` was used for the question runs. It produced schema-valid arguments and
real `tool_calls` on every question below, so the stronger `deepseek-v4-pro:cloud` was not needed.
Cloud models are reached through the local Ollama endpoint; no API key was used and none is recorded
here.

## Evidence state used for the questions

The questions ran against the **same accepted state ticket 08 used**, copied from its `.demo` root and
verified before reuse rather than rebuilt:

```bash
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli index-search \
  --out <root>/out --index <root>/out/search.db
# indexed 590 of 590 chunks from 15 documents into <root>/out/search.db
```

The copied `out/run-summary.json` records **15 published documents, 310 page records, 4 exclusions**
(`dropped=2, exact-duplicate=1, near-duplicate=1`) for engine `pymupdf@1.28.2`, and this run's
`index-search` stored 590 of 590 declared chunks from those 15 documents, so the index and the declared
page exports agree. The two dropped Documents are the image-only scans, which have no `pymupdf` text
layer. This is therefore **text-layer evidence, not OCR evidence**; the Docling
run with 17 published Documents / 313 pages is measured separately in the
[runbook](pdf-demo-runbook.md). The questions in [agent-questions.md](agent-questions.md) avoid the
scans for that reason.

The six questions were run twice each with:

```bash
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli ask \
  --question "<question>" --base-url 'http://[::1]:11434/v1' \
  --model deepseek-v4.1-flash:cloud \
  --out <root>/out --index <root>/out/search.db --max-tool-calls 6
```

## Question results

Two runs per question, `run4` and `run5`, both with the enforced contract. Every citation below is
`verified: true` in `--json`, i.e. `read_pdf_page` returned that exact page as accepted. Every cited
page is the known supporting page listed in [agent-questions.md](agent-questions.md).

| # | Answer | Structured citation | Page verified by a page read? | Tool calls (run4/run5) | Wall run4 / run5 |
| --- | --- | --- | --- | ---: | ---: |
| 1 | Four Essential Elements and four Supporting Practices — correct | `9bf665589f7333a8 p.8` | **yes**, exactly the supporting page | 2 / 2 | 2.09 s / 1.85 s |
| 2 | At least 35 mph over 24 hours — correct | `6dd127bebc737740 p.5` | **yes** | 2 / 2 | 2.39 s / 1.73 s |
| 3 | Entire bay warmer than the 2007–2023 average, with the buoy record — correct, no contradictory tail | `24564fdbfbb89e55 p.3` | **yes** | 3 / 3 | 2.89 s / 2.85 s |
| 4 | The Tribal Consultation Official — correct | `0adb664b1fafd254 p.37` | **yes**, structure replaced the prose "page 37" | 2 / 2 | 2.62 s / 2.49 s |
| 5 | Termination by Consent — correct | `8cc4c231717ef28f p.33` | **yes** | 2 / 2 | 2.05 s / 2.12 s |
| 6 | `INSUFFICIENT EVIDENCE` — correct behaviour, no citation | none | n/a | 4 / 3 | 2.56 s / 3.41 s |

Neither run needed the repair turn to reach these answers, and neither exhausted the budget. A third
instrumented run counted the model turns directly: each question took **3 model calls and 0 repair
turns** (`search`, `read`, answer). The repair turn and the insufficient-evidence fallback are pinned
by tests instead: a run that needs them is a model finding, and the loop's answer is then
`INSUFFICIENT EVIDENCE`, never an unverified citation.

### Q1: correct page, previously the wrong page

```text
[tool 1] search_pdfs {"query": "Meaningful Watershed Educational Experience essential elements supporting practices", "top_k": 5}
  -> 5 hits: 9bf665589f7333a8 p.8, 9bf665589f7333a8 p.15, 9bf665589f7333a8 p.11, +2 more
[tool 2] read_pdf_page {"doc_id": "9bf665589f7333a8", "page_number": 8}
  -> accepted page 8 of 9bf665589f7333a8 (1800 chars)

A Meaningful Watershed Educational Experience (MWEE) consists of **four Essential Elements and four
Supporting Practices** [9bf665589f7333a8 p.8].

Citations:
- 9bf665589f7333a8 p.8  https://www.noaa.gov/.../Facilitators-Guide-to-MWEE-Training-2023.pdf  (page read)
```

The first run's `p.10` citation is gone: the model read `p.8`, and a citation for any page it had not
read would have been stripped (and repaired once) rather than printed.

### Q2: correct, verified page

```text
[tool 1] search_pdfs {"query": "rapid intensification hurricane wind speed increase 24 hours", "top_k": 5}
  -> 5 hits: 6dd127bebc737740 p.5, 6dd127bebc737740 p.7, 797ef0bf9b4eef55 p.9, +2 more
[tool 2] read_pdf_page {"doc_id": "6dd127bebc737740", "page_number": 5}
  -> accepted page 5 of 6dd127bebc737740 (1361 chars)

**Rapid intensification** is defined as a hurricane's wind speeds increasing **at least 35 mph over a
24-hour period** [6dd127bebc737740 p.5].
```

The first run cited `p.5` from a snippet only; this run read it, so the citation is verified.

### Q3: correct, no contradictory tail

```text
[tool 1] search_pdfs {"query": "Chesapeake Bay sea surface temperature anomaly winter satellite", "top_k": 5}
  -> 5 hits: 24564fdbfbb89e55 p.1, p.3, p.1, +2 more
[tool 2] search_pdfs {"query": "sea surface temperature anomalies long-term average", "top_k": 5}
  -> 5 hits: 24564fdbfbb89e55 p.3, p.1, p.4, +2 more
[tool 3] read_pdf_page {"doc_id": "24564fdbfbb89e55", "page_number": 3}
  -> accepted page 3 of 24564fdbfbb89e55 (1206 chars)

Satellite sea surface temperature anomalies show the entire Chesapeake Bay was warmer this winter than
the long-term average (2007–2023) [24564fdbfbb89e55 p.3]. ... [detail from the same page, each claim
cited p.3]
```

The first run's trailing `INSUFFICIENT EVIDENCE: <what is missing>` after a supported answer is dropped
by the contract if a model emits it again; the answer above has none.

### Q4: correct, structured citation instead of prose

```text
[tool 1] search_pdfs {"query": "principal responsibility for implementing the Order and the Tribal Consultation Policy", "top_k": 5}
  -> 5 hits: 0adb664b1fafd254 p.37, p.36, p.44, +2 more
[tool 2] read_pdf_page {"doc_id": "0adb664b1fafd254", "page_number": 37}
  -> accepted page 37 of 0adb664b1fafd254 (2221 chars)

The **Tribal Consultation Official** holds principal responsibility for implementing the Order and the
Tribal Consultation Policy [0adb664b1fafd254 p.37].
```

"page 37" in prose would not be a citation; the model read the page and wrote the structured tag.

### Q5: correct, verified page

```text
[tool 1] search_pdfs {"query": "Award Action Request recipient finishes project tasks significantly prior to award end date", "top_k": 5}
  -> 5 hits: 8cc4c231717ef28f p.33, p.2, p.10, +2 more
[tool 2] read_pdf_page {"doc_id": "8cc4c231717ef28f", "page_number": 33}
  -> accepted page 33 of 8cc4c231717ef28f (1335 chars)

The **Termination by Consent** Award Action Request is the one to use. ... [8cc4c231717ef28f p.33]
```

### Q6: insufficient evidence, no citation

```text
[tool 1] search_pdfs {"query": "Annapolis buoy sea surface temperature February 2024", "top_k": 5}
  -> 5 hits: 24564fdbfbb89e55 p.3, p.4, p.5, +2 more
[tool 2] search_pdfs {"query": "sea surface temperature degrees Celsius Annapolis 1 February 2024", "top_k": 5}
  -> 5 hits: 24564fdbfbb89e55 p.3, p.4, p.5, +2 more
[tool 3] read_pdf_page {"doc_id": "24564fdbfbb89e55", "page_number": 4}
  -> accepted page 4 of 24564fdbfbb89e55 (583 chars)
[tool 4] read_pdf_page {"doc_id": "24564fdbfbb89e55", "page_number": 3}
  -> accepted page 3 of 24564fdbfbb89e55 (1206 chars)

INSUFFICIENT EVIDENCE: The corpus contains only a qualitative seasonal summary — the Annapolis buoy
water temperature is presented as a figure and described only in relative terms, with no numeric sea
surface temperature value for 1 February 2024.
```

The nearest pages were retrieved and read, the exact measurement is absent, and the model reported
that rather than inventing a number. The reply carries no citation markers, and `--json` reports
`insufficient_evidence: true` with `citations: []`.

## Limitations observed

* **Citation validity is enforced; answer correctness is not.** The loop guarantees that every
  citation names a page `read_pdf_page` returned, that prose page numbers are not citations, and that
  an unsupported answer becomes `INSUFFICIENT EVIDENCE`. It cannot make any model reason correctly, and
  it does not verify that the quoted sentence is on the cited page — the reader still has to check the
  page text the trace shows.
* **One model, two runs per question.** `deepseek-v4.1-flash:cloud` passed all six questions twice;
  `qwen3:1.7b` failed three of six on the same evidence. This is a model-quality comparison, not a
  success-rate claim: a later run can differ, and one run per question is not a benchmark.
* **Cloud models are a hosted dependency.** They are reached through the local Ollama endpoint, so the
  answers depend on the remote service being reachable and on the model revision it serves.
* **Text-layer only.** This evidence state has no OCR pages: the two scans were dropped. None of the
  claims here is OCR-quality evidence.
* **No retrieval-quality claim.** The 15-Document index is a demo, not a corpus-scale relevance
  benchmark. Lexical BM25 over the accepted chunks is the only retriever.
