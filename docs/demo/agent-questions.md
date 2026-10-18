# Agent questions: five answerable and one unanswerable

The agent test set for the P3 stage. Each answerable question names the accepted page that supports it;
the sixth question has no supporting page in the Corpus and must produce an explicit
insufficient-evidence answer rather than a citation.

These questions were run with `deepseek-v4.1-flash:cloud` over the local Ollama OpenAI-compatible
endpoint through `finepdfs ask`, against a local replay of the pinned `CC-MAIN-2026-39` manifest. That
run used the `pymupdf` text layer only, so it published 15 Documents / 310 page records / 590 chunks and
dropped the two image-only scans (no embedded text); the Docling run measured in
[`pdf-demo-runbook.md`](pdf-demo-runbook.md) publishes 17 Documents / 313 pages. The agent evidence in
[`agent-evidence.md`](agent-evidence.md) is therefore text-layer evidence, not OCR evidence, and the
questions below avoid the scans on purpose.

Each answerable question must end with a structured citation naming a page `read_pdf_page` returned;
the six answers below passed twice each with exactly the supporting page listed in the table. The
unanswerable question must end with `INSUFFICIENT EVIDENCE` and no citation. Model-quality failures are
not edited into a pass: the earlier `qwen3:1.7b` run missed on questions 1, 3 and 4, and that run is
recorded in [`agent-evidence.md`](agent-evidence.md).

`doc_id` is the Source Identity: the first 16 hex digits of the SHA-256 of the source URL. Page numbers
are original 1-based PDF pages, read from the declared page exports, never reconstructed from Markdown.

| # | Question | Known supporting page | Accepted answer |
| --- | --- | --- | --- |
| 1 | How many Essential Elements and how many Supporting Practices does a Meaningful Watershed Educational Experience (MWEE) consist of? | `9bf665589f7333a8` p.8 — *Facilitators-Guide-to-MWEE-Training-2023.pdf* | Four Essential Elements and four Supporting Practices. |
| 2 | What rate of wind-speed increase over 24 hours counts as rapid intensification for a hurricane? | `6dd127bebc737740` p.5 — *NOAA_in_the_Caribbean_Summer_2022_Newsletter_English.pdf* | At least 35 mph over a 24-hour period. |
| 3 | What do satellite sea surface temperature anomalies show for Chesapeake Bay this winter, compared with the long-term average? | `24564fdbfbb89e55` p.3 — *NCBOWinter202324SeasonalSummary.pdf* | The entire Chesapeake Bay was warmer than the long-term average (2007–2023), with buoy temperatures generally warmer from mid-December to mid-January, then below average by late January. |
| 4 | Who has principal responsibility for implementing the Order and the Tribal Consultation Policy? | `0adb664b1fafd254` p.37 — *NOAA_Tribal_Consultation_Handbook 2023_FINAL.pdf* | The Tribal Consultation Official. |
| 5 | Which Award Action Request may be used if the Recipient finishes the project tasks significantly prior to the award end date? | `8cc4c231717ef28f` p.33 — *AwardActionRequestGuidance_MAR2022B.pdf* | Termination by Consent. |
| 6 | What was the exact sea surface temperature in degrees Celsius at the Annapolis buoy on 1 February 2024? | none — the page reports anomalies and a warming/cooling narrative, not an exact reading | Insufficient evidence; no accepted page reports that measurement. |

Question 6 is deliberately near the subject of question 3: retrieval will return temperature pages, so a
model that answers it is inventing a value rather than reporting the evidence. The correct behaviour is
`INSUFFICIENT EVIDENCE` with no citation.
