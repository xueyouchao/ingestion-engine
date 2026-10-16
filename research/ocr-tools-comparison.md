# Document OCR & parsing — PaddleOCR-VL vs olmOCR vs MinerU vs EVIE vs Unlimited-OCR vs Docling

*Primary-source pass, 2026-10-16. Verified against: the OmniDocBench `v1.6_full` end-to-end leaderboard
(`opendatalab/OmniDocBench`, README @ `main`, updated 2026-09-11), the olmOCR-Bench table
(`allenai/olmocr` README), arXiv abstracts 2606.03264 / 2601.21957 / 2606.23050 / 2610.11553 /
2509.22186 / 2604.04771 / 2510.19817, Hugging Face model cards + `huggingface.co/api/models`,
the GitHub REST API, and PyPI JSON metadata. Vendor-self-reported numbers are marked;
everything else is third-party. No secondary write-ups were used.*

---

## Short answer: there is no single best, and two of your six aren't OCR tools

- **PaddleOCR-VL-1.6** is the best of your six on the only real third-party leaderboard: **96.34, 3rd of 36**
  entries on OmniDocBench v1.6, at 0.96B params and Apache-2.0. Best accuracy-per-dollar by a wide margin.
- **MinerU2.5-Pro** is 4th (95.75) and arrives inside **MinerU 4.0**, the most complete pip-installable
  pipeline (four tiers, ~12 input formats, 9 output formats, runs on CPU too).
- **Unlimited-OCR** (3.34B, **MIT**) scores 94.00 — mid-table per *page*, because it is built for a
  different unit of work: parsing a whole multi-page document in one shot.
- **olmOCR 2** is your English-print specialist: **82.4 on olmOCR-Bench** (top independently-run score),
  but ~28th of 36 on OmniDocBench (85.74). The two benchmarks disagree on purpose.
- **Docling** is not a model, it's the ingestion framework (MIT code). Stock accuracy is not comparable
  to the VLMs above; its own VLM is a 258M English-only model. It is absent from the end-to-end leaderboard.
- **EVIE** is a **visual document retrieval** model (Tencent, Apache-2.0). It outputs page *vectors*, not
  text. Putting it in an OCR bake-off is a category error.

---

## The table that settles most of it — OmniDocBench v1.6_full (third-party)

End-to-end PDF-page parsing, Markdown output, `v1.6_full` split. `Overall = ((1 − TextEdit)×100 + TableTEDS + FormulaCDM) / 3`,
so the mix is **50% text / 25% table / 25% formula**. Snapshot of the full 36-row leaderboard as published
(README updated 2026-09-11); rows for your six are in bold.

| Method | Type | Size | Overall↑ | Text Edit↓ | Formula CDM↑ | Table TEDS↑ | Table TEDS-S↑ | Read-order Edit↓ |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| TeleOCR | Specialized VLM | 1.2B | 96.91 | 0.0267 | 96.5895 | 96.8183 | 98.1806 | 0.1184 |
| OvisOCR2 | Specialized VLM | 0.8B | 96.47 | 0.0265 | 97.4854 | 94.5842 | 96.9768 | 0.1120 |
| **PaddleOCR-VL-1.6** | Specialized VLM | 0.9B | **96.34** | 0.0326 | 97.5304 | 94.7619 | 97.1002 | 0.1278 |
| **MinerU2.5-Pro** | Specialized VLM | 1.2B | **95.75** | 0.036 | 97.45 | 93.42 | 95.92 | 0.120 |
| GLM-OCR | Specialized VLM | 0.9B | 95.22 | 0.044 | 97.18 | 92.83 | 95.39 | 0.133 |
| PaddleOCR-VL-1.5 | Specialized VLM | 0.9B | 94.93 | 0.038 | 96.89 | 91.67 | 94.37 | 0.130 |
| PaddleOCR-VL | Specialized VLM | 0.9B | 94.18 | 0.040 | 95.91 | 90.65 | 93.74 | 0.135 |
| **Unlimited-OCR** | Specialized VLM | 3B | **94.00** | 0.0394 | 95.7234 | 90.21 | 93.3565 | 0.1281 |
| Youtu-Parsing | Specialized VLM | 2.5B | 93.74 | 0.044 | 93.63 | 92.02 | 95.00 | 0.116 |
| Qianfan-OCR | Specialized VLM | 4B | 93.90 | 0.04 | 95.08 | 90.53 | 93.31 | 0.13 |
| Ovis2.6-30B-A3B | General VLM | 30B | 93.70 | 0.035 | 95.17 | 89.44 | 92.40 | 0.135 |
| Logics-Parsing-v2 | Specialized VLM | 4B | 93.33 | 0.041 | 95.65 | 88.42 | 91.98 | 0.137 |
| ABot-OCR | Specialized VLM | 2B | 93.30 | 0.037 | 94.86 | 88.69 | 91.87 | 0.137 |
| FireRed-OCR | Specialized VLM | 2B | 93.26 | 0.037 | 95.44 | 88.04 | 91.06 | 0.131 |
| **MinerU-2.5** | Specialized VLM | 1.2B | 93.04 | 0.045 | 95.77 | 87.88 | 91.47 | 0.130 |
| Gemini 3 Pro | General VLM | — | 92.91 | 0.064 | 95.99 | 89.15 | 92.96 | 0.165 |
| Gemini 3 Flash | General VLM | — | 92.62 | 0.066 | 95.16 | 89.29 | 93.51 | 0.172 |
| dots.ocr | Specialized VLM | 3B | 90.77 | 0.048 | 89.95 | 87.18 | 90.58 | 0.138 |
| OpenDoc-0.1B | Specialized VLM | 0.1B | 90.67 | 0.049 | 93.02 | 83.88 | 87.45 | 0.140 |
| DeepSeek-OCR 2 | Specialized VLM | 3B | 90.25 | 0.050 | 91.84 | 83.89 | 87.75 | 0.144 |
| HunyuanOCR | Specialized VLM | 1B | 89.95 | 0.088 | 87.68 | 91.01 | 93.23 | 0.171 |
| Qwen3-VL-235B | General VLM | 235B | 89.78 | 0.063 | 92.55 | 83.07 | 86.75 | 0.166 |
| Dolphin-v2 | Specialized VLM | 3B | 89.50 | 0.069 | 91.01 | 84.40 | 87.44 | 0.150 |
| OCRVerse | Specialized VLM | 4B | 88.60 | 0.063 | 89.61 | 82.44 | 86.27 | 0.163 |
| MonkeyOCR-pro-3B | Specialized VLM | 3B | 88.57 | 0.074 | 88.74 | 84.35 | 88.62 | 0.189 |
| GPT-5.2 | General VLM | — | 86.59 | 0.114 | 88.21 | 82.95 | 87.93 | 0.193 |
| Dolphin-1.5 | Specialized VLM | 0.3B | 86.52 | 0.094 | 87.49 | 81.43 | 84.82 | 0.167 |
| MinerU-Pipeline (3.4.0) | Pipeline | — | 86.47 | 0.055 | 83.07 | 81.88 | 88.68 | 0.153 |
| **olmOCR** | Specialized VLM | 7B | **85.74** | 0.139 | 88.10 | 83.00 | 87.17 | 0.216 |
| Mistral OCR | Specialized VLM | — | 85.66 | 0.097 | 89.91 | 76.78 | 80.93 | 0.171 |
| Kimi K2.5 | General VLM | 1T | 84.53 | 0.107 | 83.50 | 80.76 | 84.00 | 0.211 |
| InternVL3.5-241B | General VLM | 241B | 83.76 | 0.130 | 89.95 | 74.35 | 79.78 | 0.215 |
| Nanonets-OCR-s | Specialized VLM | 3B | 83.61 | 0.108 | 81.46 | 80.18 | 84.51 | 0.213 |
| POINTS-Reader | Specialized VLM | 3B | 83.37 | 0.096 | 85.72 | 73.98 | 77.40 | 0.198 |
| Marker | Pipeline | — | 78.44 | 0.157 | 85.24 | 65.77 | 73.24 | 0.243 |

*Source: `raw.githubusercontent.com/opendatalab/OmniDocBench/main/README.md`, table `<caption>Comprehensive evaluation of document parsing on OmniDocBench (v1.6_full)</caption>`.*

**The framing fact:** a 0.8B–1.2B specialist (96.9 / 96.5 / 96.3) beats Gemini 3 Pro (92.91),
Qwen3-VL-235B (89.78), GPT-5.2 (86.59) and Kimi K2.5 at 1T params (84.53). Using a frontier general VLM
for OCR is no longer the accuracy play.

---

## 1. PaddleOCR-VL — best accuracy-per-dollar of the six

| Fact | Value | Source |
|---|---|---|
| Maker | Baidu / PaddlePaddle | HF card `PaddlePaddle/PaddleOCR-VL-1.6` |
| Versions | VL (2025-10-16) → 1.5 (2026-01-29) → **1.6 (2026-05-28)** | HF card news; arXiv 2601.21957, 2606.03264 |
| Params | **958,588,736** (0.96B; marketed "0.9B") | `huggingface.co/api/models/PaddlePaddle/PaddleOCR-VL-1.6` |
| License (weights) | **Apache-2.0**, not gated | same |
| Repo | `PaddlePaddle/PaddleOCR`, Apache-2.0, **91,196 stars**, last push 2026-09-16 | GitHub REST API |
| Package | `paddleocr` **3.7.0** (2026-06-11); model needs `paddleocr[doc-parser]>=3.6.0` + PaddlePaddle ≥ 3.2.1 | PyPI JSON; HF card |
| Third-party score | **96.34** OmniDocBench v1.6 (3rd/36) | OmniDocBench README |
| Self-reported | 96.33 on OmniDocBench v1.6; SOTA on v1.5 and Real5-OmniDocBench | HF card; arXiv 2606.03264 |
| Serving | vLLM (`paddleocr genai_server --backend vllm`), plus community **GGUF** and **MLX 4-bit** builds | HF card; HF search |
| Extra capabilities | seal/stamp recognition, text spotting, charts, ancient Chinese docs, rare characters, tables, formulas; en/zh/multilingual | HF card, arXiv 2601.21957 |

Why it matters for real scans: 1.5 introduced **Real5-OmniDocBench**, covering scanning, skew, warping,
screen photography and illumination — the only candidate here with a published SOTA claim on
*physical distortion*, not just clean PDFs.

Self-reported 96.33 vs third-party 96.34 is a rare and reassuring agreement.

## 2. MinerU — the best turnkey pipeline; now at 4.0

| Fact | Value | Source |
|---|---|---|
| Maker | OpenDataLab / Shanghai AI Lab | repo `opendatalab/MinerU` |
| Current release | **MinerU 4.0**; PyPI `mineru` **4.0.12** (2026-10-11), Python ≥3.10,<3.15 | repo README; PyPI JSON |
| Repo | **81,840 stars**, last push 2026-10-14 | GitHub REST API |
| Parsing tiers | Flash (preview/indexing), Basic (OCR+model), Standard, Advanced | repo README |
| Inputs | PDF, images, DOC/DOCX, PPT/PPTX, XLS/XLSX, RTF, ODT/ODS/ODP, EPUB, OFD, HTML/MHTML, CSV/TSV | repo README |
| Outputs | Markdown, HTML, LaTeX, DOCX, EPUB, PDF, Structured Content, Content List V1/V2 | repo README |
| Small models | ONNX or Torch | repo README |
| VLM backends | llama.cpp, **vLLM**, LMDeploy; default install uses Vulkan and works on CPU | repo README |
| VRAM | basic/ONNX ≈0.8 GB models, CPU OK; standard/advanced ≈2 GB models, 8 GB VRAM (CPU/Vulkan OK); PyTorch path recommends 16 GB (8 GB+ VRAM) | repo README table |
| Weights: MinerU2.5 | 1,156,026,624 params, **AGPL-3.0** ⚠ | HF API `opendatalab/MinerU2.5-2509-1.2B` |
| Weights: MinerU2.5-Pro | 1,156,026,624 params, **Apache-2.0** | HF API `opendatalab/MinerU2.5-Pro-2605-1.2B` |
| Repo license | Apache-2.0 **plus additional terms** (`LicenseRef-MinerU-Open-Source-`) | `LICENSE.md`; PyPI classifier |
| Third-party score | MinerU2.5-Pro **95.75** (4th); MinerU-2.5 93.04; MinerU-Pipeline 86.47 | OmniDocBench README |
| Papers | MinerU2.5 arXiv 2509.22186; MinerU2.5-Pro arXiv 2604.04771 (2026-04-06) | arXiv API |

**Two license traps.** (1) The repo is *not* plain AGPL: `LICENSE.md` = Apache-2.0 + additional terms —
commercial use is free **until** you exceed **100M MAU** or **USD 20M monthly revenue**, at which point a
separate commercial license is required; online services must also attribute MinerU. GitHub's API reports
`NOASSERTION` for exactly this reason. (2) The leaderboard's `MinerU-2.5` row (93.04) is the **AGPL-3.0**
weights; only **Pro** is Apache-2.0. Easy to mix up when picking a version to ship.

MinerU2.5's own contribution is a coarse-to-fine design — layout on downsampled pages, then recognition on
native-resolution crops — and Pro's is purely data engineering (65.5M samples, architecture unchanged).

## 3. Unlimited-OCR — different unit of work (whole document, one pass)

| Fact | Value | Source |
|---|---|---|
| Maker | Baidu | repo `baidu/Unlimited-OCR`; arXiv 2606.23050 |
| Released | repo 2026-06-18, paper 2026-06-22 | GitHub API; arXiv API |
| Params | **3,336,106,240** (3.34B; leaderboard says 3B) | HF API `baidu/Unlimited-OCR` |
| License | **MIT** — the most permissive of the six, not gated | same |
| Repo traction | **27,515 stars in ~4 months** | GitHub API |
| HF traction | **1,163,009 downloads**, 4,707 likes; plus AWQ (~1.09M), GGUF, MLX, mxfp4/8 mirrors | HF API; HF search |
| Core trick | **Reference Sliding-Window Attention (R-SWA)** replaces decoder attention to emulate human working memory, so KV cache and throughput stop degrading as output grows | arXiv 2606.23050 |
| Modes | single image ("gundam"/"base") and `infer_multi` for multi-page/PDF; 32k context | HF card |
| Serving | official vLLM docker images (`unlimited-ocr`, `unlimited-ocr-cu129`), SGLang, ms-swift training, Baidu Cloud, ModelScope | HF card |
| Lineage | builds on DeepSeek-OCR / DeepSeek-OCR-2 | HF card acknowledgements |
| Third-party score | **94.00** (9th/36); Text Edit 0.0394, CDM 95.7234, TEDS 90.21 | OmniDocBench README |

Read the score in context: it is measured *per page*, while the model is designed for long-horizon
multi-page parsing. Its 94.00 is mid-table here and its value proposition isn't visible in this metric at all.
The HF card does not publish VRAM requirements.

## 4. olmOCR 2 — English print specialist, a different benchmark entirely

| Fact | Value | Source |
|---|---|---|
| Maker | Allen Institute for AI (AI2) | repo `allenai/olmocr` |
| Model | `allenai/olmOCR-2-7B-1025`, Apache-2.0, not gated, **8,292,166,656** params (~8.3B) | HF API |
| Paper | olmOCR 2: Unit Test Rewards for Document OCR, arXiv **2510.19817** (2025-10-22) | arXiv API |
| Repo | **19,747 stars**, Apache-2.0, last push **2026-03-25** (quietest of the six) | GitHub REST API |
| Package | `olmocr` **0.4.27** (2026-03-12) | PyPI JSON |
| Method | RLVR with binary "unit test" rewards over synthetic documents with HTML ground truth | arXiv 2510.19817 |
| Hardware | local inference needs **≥12 GB GPU RAM** (tested RTX 4090 / L40S / A100 / H100); or point the client at a remote vLLM server | repo README |
| Own benchmark | **olmOCR-Bench: 82.4 ± 1.1** (v0.4.0) | repo README |
| Third-party (OmniDocBench) | **85.74** (~28th/36); Text Edit 0.139 vs 0.033 for PaddleOCR-VL-1.6; read-order edit 0.216 | OmniDocBench README |

olmOCR-Bench (7,000+ cases, 1,400 documents) with the third-party rows marked `*` = **vendor-supplied,
not reproduced by AI2**:

| System | ArXiv | Oldscansmath | Tables | Oldscans | Headers&footers | Multicolumn | Longtinytext | Base | Overall |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Chandra OCR 0.1.0* | 82.2 | 80.3 | 88.0 | 50.4 | 90.8 | 81.2 | 92.3 | 99.9 | 83.1±0.9 |
| Infinity-Parser 7B* | 84.4 | 83.8 | 85.0 | 47.9 | 88.7 | 84.2 | 86.4 | 99.8 | 82.5±? |
| **olmOCR v0.4.0** | 83.0 | 82.3 | 84.9 | 47.7 | 96.1 | 83.7 | 81.9 | 99.7 | **82.4±1.1** |
| PaddleOCR-VL* | 85.7 | 71.0 | 84.1 | 37.8 | 97.0 | 79.9 | 85.7 | 98.5 | 80.0±1.0 |
| Marker 1.10.1 | 83.8 | 66.8 | 72.9 | 33.5 | 86.6 | 80.0 | 85.7 | 99.3 | 76.1±1.1 |
| DeepSeek-OCR | 77.2 | 73.6 | 80.2 | 33.3 | 96.1 | 66.4 | 79.4 | 99.8 | 75.7±1.0 |
| MinerU 2.5.4* | 76.6 | 54.6 | 84.9 | 33.7 | 96.6 | 78.2 | 83.5 | 93.7 | 75.2±1.1 |
| Mistral OCR API | 77.2 | 67.5 | 60.6 | 29.3 | 93.6 | 71.3 | 77.1 | 99.4 | 72.0±1.1 |
| Nanonets-OCR2-3B | 75.4 | 46.1 | 86.8 | 40.9 | 32.1 | 81.9 | 93.0 | 99.6 | 69.5±1.1 |

*Source: `allenai/olmocr` README benchmark table.*

**Do not read these two tables as contradictory.** olmOCR is tuned for English digitised print and plain
linear text; OmniDocBench is zh/en with 50% of its weight on tables+formulas. On olmOCR's home turf
olmOCR leads everything independently run; on OmniDocBench it is ~28th. Pick by corpus, not by trophy.

## 5. Docling — not a model; the framework, and it has no leaderboard seat

| Fact | Value | Source |
|---|---|---|
| Maker | IBM Research / docling-project | repo `docling-project/docling` |
| Code license | **MIT** (models carry their own licenses) | repo README §License |
| Package | `docling` **2.139.0** (2026-10-14), `docling-serve` **1.37.0** | PyPI JSON |
| Repo | **68,959 stars** but **887 open issues**, last push 2026-10-16 (busiest of the six) | GitHub REST API |
| Scope | PDF, DOCX, PPTX, XLSX, HTML, EPUB, ODF, Apple Pages/Numbers/Keynote, email (EML/MSG), LaTeX, CSV, images, audio (WAV/MP3), **video (MP4/AVI/MOV/MKV/WebM)**, XBRL | repo README |
| Output | unified **DoclingDocument** → Markdown, HTML, DocTags, lossless JSON, WebVTT, DocLang, USPTO, JATS, XBRL | repo README |
| Ecosystem | LangChain, LlamaIndex, CrewAI, Haystack; MCP server; `docling-serve` API | repo README |
| Its own VLM | `ibm-granite/granite-docling-258M` — Apache-2.0, **257,517,120** params, **English only**, Idefics3 + SigLIP2-base + Granite 165M, released 2025-09-17 | HF API; HF card |
| Other models | SmolDocling-256M-preview; layout/table: `docling-layout-heron` (Apache-2.0, ~1.8M HF 30-day downloads), CodeFormula | HF search |
| OmniDocBench | **absent from the end-to-end table**; Docling appears only in component-level tables, linked via `docling-layout-heron` | OmniDocBench README (grep) |

So Docling's value is *breadth and plumbing*: format zoo, one document representation, exports, agent/MCP
integration, local/air-gapped execution, permissive code license. Its stock accuracy is whatever backend you
configure — a 258M English VLM by default, or any other OCR/VLM you plug in. Any "Docling beats X" claim
needs your own evaluation.

⚠️ **Trap:** the `docling-project` org also publishes `DeskForge-Qwen3.5-4B` / `DeskForge-Gemma4-E4B`.
Despite living under that org, **DeskForge is GUI grounding for desktop screenshots** (`pyautogui` action
output, arXiv 2610.02320), *not* document OCR. Don't mistake it for Docling's doc-parsing model.

## 6. EVIE — not OCR at all; it replaces OCR

| Fact | Value | Source |
|---|---|---|
| Maker | Tencent | repo `Tencent/EVIE`; HF `tencent/EVIE-8B` |
| What it is | **Visual document retrieval** — `pipeline_tag: visual-document-retrieval`, `library_name: colpali-engine`, ColBERT-style multi-vector late interaction | HF card |
| Backbone | Qwen3.5; **4096-dim** token embeddings; bidirectional full attention | HF card |
| Sizes | EVIE-8B (**8,409,476,336** params) and EVIE-4.5B (student distilled from the 8B teacher) | HF API; HF card |
| License | Apache-2.0, not gated | HF API |
| Released | HF 2026-09-04; paper arXiv **2610.11553** (2026-10-08) | HF API; arXiv API |
| Self-reported | **66.75 nDCG@10 on ViDoRe V3**; validated across 138 tasks (ViDoRe V1/V2/V3 + JinaVDR) | HF card |
| Tricks | evidence-judged data governance; symmetric listwise distillation + Prefix-MRL (one checkpoint → 6 nested dims, no re-encoding); hierarchical agglomerative index compression | arXiv 2610.11553 |
| Ecosystem | `Tencent/EVIE` repo created 2026-08-17, **56 stars** — brand new | GitHub API |
| Output | page/document **vectors**. No text, no markdown, no OCR | by construction |

EVIE is the OCR-free RAG branch: embed page *images* and search them, skipping text extraction entirely.
That is a legitimate and increasingly popular architecture — but it answers "which page is relevant", never
"give me the text". It is complementary to, not comparable with, the five parsers above.

---

## Head-to-head, your six

| | **PaddleOCR-VL-1.6** | **MinerU2.5-Pro / MinerU 4.0** | **Unlimited-OCR** | **olmOCR 2** | **Docling** | **EVIE** |
|---|---|---|---|---|---|---|
| Category | doc-parsing VLM | doc-parsing VLM + pipeline | doc-parsing VLM (long-horizon) | doc-parsing VLM (English) | framework + small VLM | retrieval embedder |
| Params | 0.96B | 1.16B (+ small ONNX models) | 3.34B | 8.29B | 258M VLM | 8.4B / 4.5B |
| Weights license | Apache-2.0 | Apache-2.0 (Pro); AGPL-3.0 (2.5) | **MIT** | Apache-2.0 | Apache-2.0 (VLM) | Apache-2.0 |
| Code license | Apache-2.0 | Apache-2.0 + thresholds | MIT | Apache-2.0 | **MIT** | — |
| OmniDocBench v1.6 | **96.34 (#3)** | 95.75 (#4) | 94.00 (#9) | 85.74 (~#28) | not listed | n/a |
| olmOCR-Bench | 80.0* | 75.2* / n-a | n/a | **82.4 (best indep.)** | not listed | n/a |
| Output | Markdown/structure | 9 formats incl. MD/HTML/DOCX/JSON | Markdown + `det` tags | clean plain text / MD | DoclingDocument → 10+ | vectors |
| Multi-page | per page | per page + doc library | **one-shot whole document** | per page | per page | whole page |
| Local hardware | small GPU / GGUF / MLX | 8 GB VRAM, **or CPU** | vLLM/SGLang GPU | ≥12 GB VRAM | CPU or any backend | GPU |
| Languages | en/zh/multilingual | en/zh/multilingual | multilingual | **English-centric** | English (its VLM) | n/a |
| Best for | accuracy per $ | turnkey ingest | huge corpora, long docs | English print | formats + ecosystem | search, not extraction |

`*` = vendor-supplied number in AI2's table, not independently reproduced.

## What to pick when

- **Best raw page→Markdown accuracy on a modest GPU** → **PaddleOCR-VL-1.6**. #3 overall, best of your six,
  Apache-2.0, 0.96B, and the only one with a published distortion-robustness (Real5) SOTA.
- **You want to `pip install` one thing and stop thinking about formats** → **MinerU 4.0**, with
  **MinerU2.5-Pro** weights (Apache-2.0). Runs on CPU, four quality tiers, nine output formats, doc library,
  agent/MCP-friendly. Read the extra license terms first if you're a very large commercial operator.
- **Millions of scans, cost-sensitive, whole documents at a time, permissive license required** →
  **Unlimited-OCR** (MIT), served with vLLM. Judge it on long-document throughput, not its per-page rank.
- **English-only prose, arXiv/old scans/math, and you trust olmOCR-Bench** → **olmOCR 2**, ≥12 GB VRAM or a
  remote vLLM server. Do **not** pick it if the corpus is Chinese or table/formula-heavy.
- **Mixed Chinese+English real-world scans** (skew, photos, illumination) → **PaddleOCR-VL-1.6**
  (Real5-OmniDocBench) over olmOCR.
- **Table extraction specifically** (TEDS) → PaddleOCR-VL-1.6 **94.76** > MinerU2.5-Pro 93.42 >
  Unlimited-OCR 90.21 > olmOCR 83.00.
- **Formula-heavy** (CDM) → PaddleOCR-VL-1.6 **97.53** ≈ MinerU2.5-Pro 97.45 > Unlimited-OCR 95.72 >
  olmOCR 88.10.
- **You already run a RAG framework and value integration/format coverage** → **Docling** (MIT), optionally
  with granite-docling-258M or a stronger VLM backend. Accept that it has no leaderboard rank.
- **Your real goal is retrieval, not text** → **EVIE** (or any ColPali-style model). Skip OCR entirely.
- **Strict permissive-license shopping list** → Unlimited-OCR (MIT), Docling code (MIT), or
  PaddleOCR-VL-1.6 / MinerU2.5-Pro / olmOCR 2 / EVIE (Apache-2.0). Avoid the AGPL-3.0 MinerU2.5 weights.

## Also on the board — not in your list, but higher

You asked about six; the top of the third-party leaderboard is led by models you didn't name:

| Rank | Model | Params | Overall | Link |
|---:|---|---:|---:|---|
| 1 | **TeleOCR** | 1.2B | **96.91** | `StarDoc-AI/TeleOCR`, `github.com/caipeng328/NaviDC-OCR` |
| 2 | **OvisOCR2** | 0.8B | **96.47** | `ATH-MaaS/OvisOCR2` |
| 5 | GLM-OCR | 0.9B | 95.22 | `zai-org/GLM-OCR` |
| — | Qianfan-OCR | 4B | 93.90 | `baidu/Qianfan-OCR` |
| 19 | OpenDoc-0.1B | **0.1B** | 90.67 | — (0.1B beating Gemini 3 Pro) |

If "most powerful OCR tool" is the literal question, TeleOCR and OvisOCR2 outrank everything you listed —
both are 1B-class and very new (OvisOCR2 and TeleOCR were added to the leaderboard on 2026-09-11), so treat
their ecosystem maturity as unproven relative to MinerU/PaddleOCR.

## Verification notes, contradictions and uncertainty

- **What the headline metric does not measure:** OmniDocBench v1.6_full knows nothing about throughput,
  cost, handwriting, or deployment burden; olmOCR-Bench is English-only. Every number here is
  corpus-dependent. Re-run the shortlist on *your* documents before committing.
- **olmOCR's leaderboard checkpoint is not pinned.** The end-to-end table says only "olmOCR, 7B"; the
  README's version tables pin other models but not this one (`olmOCR` appears in a *component* eval table
  under its own name). So 85.74 may not be olmOCR-2-7B-1025 specifically. Treat it as approximate.
- **`*` rows in olmOCR-Bench are vendor-supplied** (PaddleOCR-VL, MinerU, Chandra, Infinity-Parser). They
  were not reproduced by AI2, so ranking them against olmOCR's own 82.4 is not apples-to-apples.
- **Vendor vs third-party agreement:** PaddleOCR-VL-1.6 self-reports 96.33 and the third party measured
  96.34 — good. No such cross-check was found for Unlimited-OCR or EVIE.
- **Licenses need legal review, not my summary.** MinerU: weights dual-licensed (Pro Apache-2.0 vs 2.5
  AGPL-3.0), repo Apache-2.0 + 100M MAU / USD 20M-monthly-revenue thresholds + attribution obligation;
  GitHub API reports `NOASSERTION`. That is a genuinely unusual license.
- **Docling has no end-to-end OmniDocBench seat.** Its layout model is referenced in component tables only;
  its README asserts capabilities, not rankings.
- **Could not verify in this pass:** VRAM/throughput figures for Unlimited-OCR (not published on the card);
  any head-to-head tokens/second comparison from a primary source; EVIE's applicability to text extraction
  (it produces no text by design); the 1.6→"latest" upgrade path for PaddleOCR-VL (1.6 was newest as of
  2026-10-16, but this family ships every few months).
- **Method note:** the leaderboard was parsed from the README's HTML `<table>`; I re-checked cell counts
  (9 `<td>` per row against 9 `<th>`) after an early parse silently dropped the TableTEDS-S column.

## For this repo (CocoIndex)

`research/pdf-pipeline-design.md` assumes Stage A = **docling** PDF→Markdown (memoized, "~0.5–2 s/page CPU")
and routes OCR need through **olmOCR** — spelled `rolmOCR` in `research/finepdfs-notes.md`, which is the
FinePDFs `processor` field value, same model. Three updates from this pass:

1. **The parser is now swappable on accuracy grounds.** PaddleOCR-VL-1.6 (0.96B, Apache-2.0) or
   MinerU2.5-Pro (1.16B, Apache-2.0) score **~10 points higher** than olmOCR on a zh/en table+formula
   benchmark. Both sit behind a vLLM endpoint, so they drop into the existing
   `coco.fn.as_async(runner=coco.GPU)` boundary the design already specifies. Nothing else in the two-stage
   architecture changes — that is the payoff of the design.
2. **Keep docling for breadth, not for rank.** Its format zoo (Office, email, EPUB, audio/video, XBRL) is
   wider than MinerU 4.0's and much wider than a bare VLM. The natural split: docling for odd formats,
   a top VLM parser for the PDF/scan bulk.
3. **License posture for a commercial product:** PaddleOCR-VL-1.6 / MinerU2.5-**Pro** / olmOCR 2
   (Apache-2.0) or Unlimited-OCR (MIT). Do not ship the **AGPL-3.0** MinerU2.5 (non-Pro) weights — and note
   the leaderboard's `MinerU-2.5` row is that model, while only `MinerU2.5-Pro` is Apache-2.0.

Two design-doc invariants still hold: the GPU pool note (parser and embedder fight over one GPU — use
`runner=coco.GPU` and let the engine batch), and the memoization argument, which matters *more* now that the
parser is a VLM: the OCR box is the expensive one, fingerprinted by content, so tuning a downstream filter
never re-OCRs the corpus.

**EVIE is orthogonal to this pipeline, not a component of it.** Adopting it would replace
parse→chunk→embed with page-image vectors — cheaper and layout-faithful, but it forfeits the repo's
citable-per-chunk goal, since there is no extracted text to cite.
