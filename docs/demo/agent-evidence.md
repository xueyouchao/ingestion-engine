# Agent evidence: real tool calls over accepted pages

This records what was observed, not what is assumed. It answers one question for the demo: can a real
model call the two agent tools, receive their results, and produce an answer with page citations over
the same accepted evidence the browser reads? The short answer is **yes for tool calling and the result
round trip**, with observed model-quality limits that are listed rather than hidden.

Observed on 2026-10-18 on the rehearsal host. Machine state changes; the commands are the check, not the
answers in this file.

## Environment check

```bash
curl -sS --max-time 5 'http://[::1]:11434/api/tags'
curl -sS --max-time 4 'http://127.0.0.1:11434/api/tags'
```

* The IPv6 endpoint `http://[::1]:11434` answered and listed `qwen3:1.7b` with capabilities
  `completion`, `tools`, `thinking`, plus cloud-tagged models such as `deepseek-v4.1-flash:cloud`.
* The IPv4 endpoint `127.0.0.1:11434` timed out; the demo uses the IPv6 URL.
* A model list is **not** evidence of tool support, so the next checks were actual inferences with a
  tool schema. `qwen3:1.7b` was tried first and was sufficient; no cloud-tagged model was needed.

The model is reached through the repository's stdlib client
(`finepdfs/infrastructure/llm/chat_completions.py`) at `http://[::1]:11434/v1/chat/completions`.

## Capability probe: a real tool call and round trip

A small script used the same client, a synthetic `lookup_page` schema, and a prompt that required the
tool. The response contained a real `tool_calls` array:

```json
{
  "role": "assistant",
  "content": "",
  "tool_calls": [
    {"id": "call_owp288mh", "type": "function",
     "function": {"name": "lookup_page",
                  "arguments": "{\"doc_id\":\"0123456789abcdef\",\"page_number\":3}"}}
  ]
}
```

First-call wall time was **12.12 s**. The returned arguments were valid JSON matching the schema.

The demo's own tools then ran against the real local index and the real declared page exports:

```bash
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli ask \
  --question "Use read_pdf_page on document 9bf665589f7333a8, page 8, and quote the sentence that defines what a MWEE is. Do not use search_pdfs." \
  --base-url 'http://[::1]:11434/v1' --model qwen3:1.7b \
  --out .demo/out --index .demo/out/search.db --max-tool-calls 3
```

```text
[tool 1] read_pdf_page {"doc_id": "9bf665589f7333a8", "page_number": 8}
  -> accepted page 8 of 9bf665589f7333a8 (1800 chars)

The sentence defining what a MWEE is is: "A Meaningful Watershed Educational Experience (MWEE)
consists of four Essential Elements and four Supporting Practices." This is found in document
9bf665589f7333a8 p.8.

Citations:
- 9bf665589f7333a8 p.8  https://www.noaa.gov/sites/default/files/2023-02/Facilitators-Guide-to-MWEE-Training-2023.pdf

Tool calls: 1 of 3
```

That is the requested tool-result round trip: `read_pdf_page` returned the exact 1,800-character
accepted Page Text, the model received it as a `tool` message, and the final answer quoted it. The
`search_pdfs` tool was also called by every question below; the tool trace is printed by the CLI and
included in `--json` output.

## Evidence state used for the questions

The questions ran against a local replay of the pinned `CC-MAIN-2026-39` manifest, processed with the
`pymupdf` text layer:

```bash
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo restore
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.demo run --engine pymupdf
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli index-search \
  --out .demo/out --index .demo/out/search.db
```

Observed: `records seen 19, fetched 18, already present 1, failed 0`; the run reported **19 crawl
records / 18 documents, 15 published documents, 310 page records, 590 chunks, 4 exclusions**
(`dropped=2, exact-duplicate=1, near-duplicate=1`). The two dropped Documents are the image-only scans,
which have no `pymupdf` text layer. This is therefore **text-layer evidence, not OCR evidence**; the
Docling run with 17 published Documents / 313 pages is measured separately in the
[runbook](pdf-demo-runbook.md). The questions in [agent-questions.md](agent-questions.md) avoid the
scans for that reason.

The six questions were run once each with:

```bash
PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli ask \
  --question "<question>" --base-url 'http://[::1]:11434/v1' --model qwen3:1.7b \
  --out .demo/out --index .demo/out/search.db --max-tool-calls 6
```

## Question results

| # | Answer | Structured citation | Cited page verified by a page read? | Tool calls | Wall |
| --- | --- | --- | --- | ---: | ---: |
| 1 | Four Essential Elements and four Supporting Practices — correct | `9bf665589f7333a8 p.10` | no; **the cited page is wrong** (the sentence is on p.8, which was also returned) | 1 | 78 s |
| 2 | At least 35 mph over 24 hours — correct | `6dd127bebc737740 p.5` | no (snippet only) | 1 | 83 s |
| 3 | Entire bay warmer than the 2007–2023 average — correct, but the answer also appends a contradictory `INSUFFICIENT EVIDENCE` sentence | `24564fdbfbb89e55 p.1`, `p.3` | no (snippet only) | 1 | 112 s |
| 4 | The Tribal Consultation Official — correct | none structured; the prose says "page 37" | n/a | 1 | 94 s |
| 5 | Termination by Consent — correct | `8cc4c231717ef28f p.33` | **yes** (`read_pdf_page` returned the page) | 2 | 120 s |
| 6 | `INSUFFICIENT EVIDENCE` — correct behaviour | none | n/a | 1 | 126 s |

Citations are filtered to pages a tool actually returned. The `verified` flag is true only when
`read_pdf_page` returned that exact page; a snippet citation still names returned evidence, but it does
not prove the cited page says what the answer claims. The CLI prints `(snippet only; page not read)` or
`(page read)` beside every citation, and `--json` includes the flag.

### Q1: correct answer, wrong page attribution

```text
[tool 1] search_pdfs {"query": "Essential Elements Supporting Practices MWEE", "top_k": 5}
  -> 5 hits: 9bf665589f7333a8 p.15, 9bf665589f7333a8 p.11, 9bf665589f7333a8 p.8, +2 more

A Meaningful Watershed Educational Experience (MWEE) consists of **4 Essential Elements** and **4
Supporting Practices**. This information is explicitly stated in the fifth search result:
[9bf665589f7333a8 p.10], "A Meaningful Watershed Educational Experience (MWEE) consists of four
Essential Elements and four Supporting Practices."

Citations:
- 9bf665589f7333a8 p.10  https://www.noaa.gov/.../Facilitators-Guide-to-MWEE-Training-2023.pdf  (snippet only; page not read)
```

`p.10` does **not** contain the quoted sentence; the declared page export for `p.8` does. The answer is
grounded in a returned snippet, but the page attribution is the model's error. The system prompt asks
for a `read_pdf_page` before citing, and `qwen3:1.7b` did not comply on this question. This is exactly
why a snippet-only citation is reported as unverified rather than presented as proof.

### Q2: correct, snippet citation

```text
[tool 1] search_pdfs {"query": "rapid intensification hurricane threshold 24 hours", "top_k": 5}
  -> 5 hits: 6dd127bebc737740 p.5, 6dd127bebc737740 p.7, 797ef0bf9b4eef55 p.9, +2 more

The rate of wind-speed increase over 24 hours that counts as rapid intensification for a hurricane is
at least **35 mph**. This threshold is explicitly mentioned in the document [6dd127bebc737740 p.5].
```

### Q3: correct answer, contradictory trailing sentence

```text
[tool 1] search_pdfs {"query": "Chesapeake Bay sea surface temperature anomalies", "top_k": 5}
  -> 5 hits: 24564fdbfbb89e55 p.1, 24564fdbfbb89e55 p.3, 24564fdbfbb89e55 p.1, +2 more

The satellite sea surface temperature anomalies for Chesapeake Bay this winter show the entire bay
being warmer compared to the long-term average ... [24564fdbfbb89e55 p.1] and [24564fdbfbb89e55 p.3].
...
INSUFFICIENT EVIDENCE: <what is missing>
The tools do not provide specific numerical data or detailed temporal comparisons, but the search
results explicitly state the warming trend.
```

The answer is correct and cites the two returned pages, but the trailing `INSUFFICIENT EVIDENCE` line is
a model contradiction. The loop's flag is based on the answer's first phrase, so `--json` reports
`insufficient_evidence: false`; the raw text is recorded here rather than smoothed over.

### Q4: correct answer, no machine-checkable citation

```text
[tool 1] search_pdfs {"query": "Order and the Tribal Consultation Policy", "top_k": 5}
  -> 5 hits: 0adb664b1fafd254 p.44, 0adb664b1fafd254 p.43, 0adb664b1fafd254 p.33, +2 more

The principal responsibility ... lies with the **Tribal Consultation Official**. This role is
explicitly outlined in the text on page 37 of the document ...
```

`p.37` was among the returned hits (the CLI prints only the first three), and its declared text does
contain that sentence, but the answer wrote "page 37" in prose without a doc_id, so the filter cannot
turn it into a structured citation. It is reported as no citation, not as a verified one.

### Q5: correct, and the page was read

```text
[tool 1] search_pdfs {"query": "Award Action Request", "top_k": 5}
  -> 5 hits: 8cc4c231717ef28f p.34, 8cc4c231717ef28f p.38, 8cc4c231717ef28f p.33, +2 more
[tool 2] read_pdf_page {"doc_id": "8cc4c231717ef28f", "page_number": 33}
  -> accepted page 33 of 8cc4c231717ef28f (1335 chars)

The Award Action Request for Termination by Consent may be used if the Recipient finishes the project
tasks significantly prior to the award end date ...

Citation: [8cc4c231717ef28f p.33]

Citations:
- 8cc4c231717ef28f p.33  https://www.noaa.gov/.../AwardActionRequestGuidance_MAR2022B.pdf  (page read)
```

This is the full intended shape: search, inspect the exact page, answer, cite a verified page.

### Q6: insufficient evidence, no citation

```text
[tool 1] search_pdfs {"query": "Annapolis buoy sea surface temperature 2024", "top_k": 5}
  -> 5 hits: 24564fdbfbb89e55 p.3, 24564fdbfbb89e55 p.4, 24564fdbfbb89e55 p.5, +2 more

INSUFFICIENT EVIDENCE: The provided documents mention that water temperatures at the Annapolis buoy
were warmer than average from mid December until mid January, then cooled to below average by late
January, and then warmed back in February. However, the exact sea surface temperature value is not
specified in the text.

Citations: none (no evidence page was cited)
Tool calls: 1 of 6
The agent reported insufficient evidence.
```

The nearest pages were retrieved, the exact measurement is absent, and the model reported that rather
than inventing a number.

## Limitations observed

* **`qwen3:1.7b` is small.** It produced one wrong page attribution (Q1), one contradictory trailing
  sentence (Q3), and one prose-only citation (Q4). It called `read_pdf_page` spontaneously only for Q5.
  These are model-quality findings, not hidden by the loop; the trace and the `verified` flag make them
  visible.
* **One run per question.** The answers are not deterministic enough to claim a success rate; the table
  records the observed runs, and a later run can differ (an earlier Q1 run answered without a citation
  at all).
* **Text-layer only.** This evidence state has no OCR pages: the two scans were dropped. None of the
  claims here is OCR-quality evidence.
* **No retrieval-quality claim.** The 15-Document index is a demo, not a corpus-scale relevance
  benchmark. Lexical BM25 over the accepted chunks is the only retriever.
* **The tool budget and citations are enforced in code; answer correctness is not.** The loop caps tool
  executions and filters citations to returned evidence, but it cannot make a 1.7B model reason
  perfectly. The honest output includes the tool trace and the verification flag.
