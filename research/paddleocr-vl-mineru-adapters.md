# PaddleOCR-VL-1.6 & MinerU2.5 — invocation and output contract for Python adapters

*Primary-source pass, 2026-10-16. Sources: Hugging Face model cards + `huggingface.co/api/models`,
the `PaddlePaddle/PaddleOCR` and `PaddlePaddle/PaddleX` GitHub sources (raw), vLLM `supported_models.md`
and the vLLM PaddleOCR-VL recipe, the PaddleOCR-VL usage docs, the `opendatalab/MinerU` repo at
`master` and tag `mineru-2.5.4-released`, the MinerU docs site, PyPI JSON, and the extracted
`mineru-4.0.12` / `mineru-vl-utils-2.0.7` wheels. No secondary write-ups used.*

---

## 1. PaddleOCR-VL (current = 1.6)

### Model ids / variants
| id | notes | license | params |
|---|---|---|---|
| `PaddlePaddle/PaddleOCR-VL-1.6` | current; arch `PaddleOCRVLForConditionalGeneration`, `model_type: paddleocr_vl` | Apache-2.0 | 958,588,736 (BF16) |
| `PaddlePaddle/PaddleOCR-VL-1.5` | previous | Apache-2.0 | 958,588,736 |
| `PaddlePaddle/PaddleOCR-VL` | v1 ("PaddleOCR-VL-0.9B" component) | Apache-2.0 | 958,588,736 |
| `PaddlePaddle/PaddleOCR-VL-1.6-GGUF`, `mlx-community/PaddleOCR-VL-1.6-4bit` | community quantizations | — | — |

The string `PaddleOCR-VL-1.6-0.9B` is the *served/VLM component name* used by
`paddleocr genai_server --model_name ...`; there is **no** HF repo `PaddlePaddle/PaddleOCR-VL-1.6-0.9B` (HTTP 401).
Sources: `huggingface.co/api/models/PaddlePaddle/*`; HF card front-matter; PaddleOCR-VL docs §intro.

### Install
```bash
python -m pip install paddlepaddle-gpu==3.2.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
python -m pip install -U "paddleocr[doc-parser]>=3.6.0"
```
PyPI `paddleocr` 3.7.0, `doc-parser` extra → `paddlex[genai-client,ocr]>=3.7.0,<3.8.0`.
Requires PaddlePaddle ≥3.2.1. Sources: HF 1.6 card; PyPI JSON.

### CLI / Python entry points
```bash
paddleocr doc_parser -i <img|pdf|dir> --pipeline_version v1.6 --save_path ./output
# local backends: (default) PaddlePaddle, or --engine transformers
# remote VLM:  --vl_rec_backend vllm-server --vl_rec_server_url http://127.0.0.1:8080/v1
#             --vl_rec_api_model_name <served name>
# device:      --device cpu|xpu|dcu|metax_gpu ...
paddleocr genai_server --model_name PaddleOCR-VL-1.6-0.9B --host 0.0.0.0 --port 8080 --backend vllm
```
```python
from paddleocr import PaddleOCRVL
p = PaddleOCRVL(pipeline_version="v1.6")                     # default is v1.6
for res in p.predict("page.png"):                            # also dir/list/PDF
    res.print(); res.save_to_json(save_path=out)
    res.save_to_markdown(save_path=out); res.save_to_word(save_path=out)
# multi-page restructure: p.restructure_pages(pages_res, merge_tables=True,
#                          relevel_titles=True, concatenate_pages=True)
```
`pipeline_version` ∈ `"v1"|"v1.5"|"v1.6"`; per-page results. Sources: HF 1.6 card; docs
`paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html`; `paddleocr/_pipelines/paddleocr_vl.py`,
`paddleocr/_cli.py`.

### vLLM / OpenAI-compatible serving
```bash
vllm serve PaddlePaddle/PaddleOCR-VL-1.6 \
  --trust-remote-code --max-num-batched-tokens 16384 \
  --no-enable-prefix-caching --mm-processor-cache-gb 0
# recipe text still names the v1 id; add --served-model-name PaddleOCR-VL-1.6-0.9B if needed
```
```python
client.chat.completions.create(
  model="PaddlePaddle/PaddleOCR-VL",          # or the served name
  messages=[{"role":"user","content":[
      {"type":"image_url","image_url":{"url": "..."}},      # or data: URL base64
      {"type":"text","text":"OCR:"}]}],                     # Table/Formula/Chart Recognition:
  temperature=0.0)
```
- Task base prompts: `OCR:`, `Table Recognition:`, `Formula Recognition:`, `Chart Recognition:`.
- `chat_template.jinja` renders `User: <|IMAGE_START|><|IMAGE_PLACEHOLDER|><|IMAGE_END|><prompt>\nAssistant:\n`; vLLM applies it automatically.
- Image processor: `min_pixels=112896`, `max_pixels=1003520`, `patch_size=14`, `merge_size=2`, mean/std 0.5. The 1.6 card's transformers spotting example raises `max_pixels` to `1605632`.
- **The genai_server serves only the VLM stage.** Layout model `PP-DocLayoutV2` must run client-side; docs "strongly discourage" calling the server directly with an OpenAI client for full parsing.
Sources: `vllm-project/recipes` PaddleOCR-VL (and docs.vllm.ai mirror); vLLM `docs/models/supported_models.md`
(`PaddleOCRVLForConditionalGeneration`); `vllm/model_executor/models/paddleocr_vl.py`; HF `chat_template.jinja`,
`preprocessor_config.json`; docs §3.

### Output shape
`res.print()` / `res.json` (after numpy→list): `{"res": {`
`input_path, page_index, page_count, width, height,`
`model_settings:{use_doc_preprocessor,use_layout_detection,use_chart_recognition,format_block_content,markdown_ignore_labels},`
`layout_det_res:{boxes:[{cls_id,label,score,coordinate:[x0,y0,x1,y1]}]},`
`parsing_res_list:[{block_label,block_content,block_bbox,block_id,block_order,group_id[,block_polygon_points]}],`
`doc_preprocessor_res? , spotting_res? }}`.
Markdown attribute: `{markdown_texts, markdown_images, page_continuation_flags}`.
Layout labels seen: `doc_title, text, image, paragraph_title, vision_footnote, table, formula, ...`.
Sources: docs §result explanation; `PaddleX/paddlex/inference/pipelines/paddleocr_vl/result.py`.

### What the CLI writes (`--save_path DIR`, input `page.png`)
`page_res.json` (the `{"res":{...}}` envelope), `page.md`, `page_layout_det_res.png`, `page.docx`;
`page.html` / `page.xlsx` only when tables are present. PDFs are processed page-by-page and each page
gets its own `.md` (and suffix-less collisions overwrite). Filenames come from the input stem.
Sources: `paddleocr/_utils/cli.py` (`save_all`), `PaddleX/.../common/result/mixin.py` (`{stem}_{key}.json`,
`{stem}.md`, `{stem}_{key}.png`, `{stem}.docx`), `.../base_result.py`.

### Full HTTP API (optional, docker "Service Deployment")
`paddleocr-vl-api` → `POST /layout-parsing` JSON `{"file": <url|base64>, "fileType": 0|1, ...}` →
`{"logId","errorCode","errorMsg","result":{"layoutParsingResults":[{"prunedResult":{...},
"markdown":{"texts","images"},"outputImages":{...}}], ...}}`; `POST /restructure-pages`.
Sources: docs §4, §4.3.

### Hardware + license
- License **Apache-2.0** (code + weights). Source: HF card, repo.
- Local PaddlePaddle: NVIDIA CC ≥7.0 / CUDA ≥11.8, plus x64 CPU, Kunlunxin XPU, Hygon DCU, MetaX, Iluvatar, Apple Silicon (MLX-VLM), AMD, Intel Arc; Huawei Ascend via vLLM.
- vLLM: CC ≥8.0, CUDA ≥12.6; SGLang/FastDeploy: 8.0 ≤ CC < 12.0. T4/V100 (CC 7.x) not recommended for vLLM.
- **No explicit minimum-VRAM number** in official docs (flag). Weights are ~1.9 GB BF16; server example tunes `gpu-memory-utilization`.
Sources: docs §"Inference Method and Hardware Support Matrix", §Environment Requirements.

---

## 2. MinerU2.5 / MinerU2.5-Pro

### Model ids / variants
| id | role | license | params |
|---|---|---|---|
| `opendatalab/MinerU2.5-Pro-2605-1.2B` | **current** default VLM in MinerU 4.0 (Standard/Advanced) | Apache-2.0 | 1,156,026,624 |
| `opendatalab/MinerU2.5-2509-1.2B` | VLM of MinerU **2.5.x** `vlm-*` backend | AGPL-3.0 | 1,156,026,624 |
| `opendatalab/MinerU2.5-Pro-2604-1.2B` | prior Pro | Apache-2.0 | 1,156,026,624 |
| `opendatalab/MinerU2.0-2505-0.9B` | old | — | — |
| `jinzhenj/MinerU2.5-Pro-2605-1.2B-GGUF` | llama.cpp GGUF (Q8_0 + mmproj) | — | — |
Small model bundles: `opendatalab/MinerU-4_models_torch`, `opendatalab/MinerU-4_models_onnx`.
Sources: HF cards/API; `mineru-4.0.12` wheel `mineru/mineru/model/registry.py`.

### Package / install (current = MinerU 4.0)
PyPI `mineru` **4.0.12** (2026-10-11), Python ≥3.10,<3.15.
```bash
uv pip install -U "mineru>=4.0,<5"          # base: ONNX CPU small models + llama.cpp VLM
uv pip install -U "mineru[full]>=4.0"       # NVIDIA: torch + vLLM for best throughput
mineru-kit models download --tier standard --source huggingface
mineru-kit models verify --tier standard
```
Old `mineru[core]` / `mineru[pipeline]` extras are gone. Sources: repo README @ master; PyPI JSON; docs
migration_4.

### CLI (current 4.0)
```bash
# stateless one-off conversion (defaults to all pages, markdown)
mineru-kit parse <input|dir> -o <out> [--format markdown|middle_json|zip] \
    [--tier flash|basic|standard|advanced] [--pages "1-5,8,r3-r1"] \
    [--ocr-mode auto|txt|ocr] [--remote | --remote-url URL --api-key K]
# document-library client (PDF defaults to first 10 pages; add --pages all)
mineru parse <input> --pages all -o out.md [--json]
mineru server start ; mineru read "doc:<id>/tier:standard/page:1" ; mineru search "..."
# serve the VLM alone (OpenAI-compatible /v1/models + /v1/chat/completions)
mineru-kit vlm-server --engine auto|vllm|lmdeploy|mlx [--port 30000]
```
`--tier` = quality, not a backend: flash = native text / OCR no VLM; basic = ONNX/Torch small models;
standard = small models + VLM (= MinerU2.5-Pro); advanced = same models, more compute. Default for
PDF/image is `standard`. `--format`: single file → `markdown:<stem>.md`, `middle_json:<stem>.json`,
`zip:<stem>.zip`. Sources: docs usage/cli_tools, usage/tiers, next/cli/mineru-kit-parse; wheel
`mineru/kit/commands/parse.py`.

### Legacy CLI (package 2.5.4 — the "2.5" the question refers to)
```bash
mineru -p <input> -o <outdir> -b pipeline|vlm-transformers|vlm-vllm-engine|vlm-http-client  # default pipeline
      -m auto|txt|ocr -l <lang> -d <cpu|cuda:0|npu|mps> -s <page> -e <page> -f BOOL -t BOOL
      --source huggingface|modelscope|local [--vram N]
mineru-openai-server --port 30000
mineru -p <input> -o <out> -b vlm-http-client -u http://127.0.0.1:30000
mineru-api --host 0.0.0.0 --port 8000
```
Later 2.7.x replaced these with `pipeline|hybrid-auto-engine|hybrid-http-client|vlm-auto-engine|vlm-http-client`
(default `hybrid-auto-engine`). Sources: `docs/en/usage/cli_tools.md` @ tag `mineru-2.5.4-released` and @
`mineru-2.7.6-released`.

### HTTP API
- **4.0** (`mineru-kit api-server --host 127.0.0.1 --port 8000 --tier standard [--api-key K] [--vlm-server-url ...]`):
  `curl 'http://127.0.0.1:8000/v1/file_parse?tier=standard&response_format=json|zip' -F 'files=@doc.pdf'`
  (sync, default 300 s wait, 202 + task URLs on timeout); `POST /v1/tasks` async; `POST /v1/uploads` →
  `PUT .../content`; `GET /v1/files/{id}/content`; `GET /v1/health|/v1/models|/v1/usage`; `/docs`.
  Limits: 200 MiB/file, 100 files/task. `mineru-api` is retained as an alias command.
- **2.5.4**: `mineru-api` → `POST /file_parse`, multipart fields `files`, `backend`, `lang_list`,
  `parse_method`, `formula_enable`, `table_enable`, `server_url`, `return_md`, `return_middle_json`,
  `return_model_output`, `return_content_list`, `return_images`, `response_format_zip`, `start_page_id`,
  `end_page_id`. Sources: docs usage/http_api; wheel `mineru/parser/task_api.py`
  (`@router.post("/file_parse")`); tag-2.5.4 `mineru/cli/fast_api.py`.

### Output artifacts
- **4.0** `ParseResult.save()` writes `markdown.md`, `middle_json.json`, `structured_content.json`,
  `images/`, plus `model_output.json`. Content List V1/V2 exist only as `mineru.render` targets
  (`RenderFormat.CONTENT_LIST`, `CONTENT_LIST_V2`).
  `middle_json`/`model_output` carry serialized `schema` identity (`docvortex.middle` / `docvortex.model`,
  protocol 2.0); legacy `_backend`/`pdf_info` fields are not the 4.0 contract.
- **2.5.x** writes `<stem>.md`, `<stem>_content_list.json`, `<stem>_middle.json`, `<stem>_model.json`,
  `<stem>_layout.pdf`, `<stem>_spans.pdf`, `images/`.
  `middle.json` top-level `{pdf_info:[{page_idx,page_size,para_blocks,images,tables,interline_equations,
  discarded_blocks,...}], _backend, _version_name}`.
  `content_list.json` is a flat reading-order list; items `{type: text|image|table|equation, page_idx,
  bbox:[x0,y0,x1,y1] (0–1000), text / text_level / text_format, img_path, img_caption, img_footnote,
  table_body, table_caption, table_footnote}`.
  Sources: docs reference/output_files @ master and @ `mineru-2.5.4-released`.
- **mineru-vl-utils (direct model use, 2.x line)**: `MinerUClient(backend="transformers"|"vllm-engine"|
  "vllm-async-engine"|"http-client"|"lmdeploy-engine"|"llama-cpp-engine"|"mlx-engine", server_url=...)
  .two_step_extract(PIL.Image)` → list of `ContentBlock` dicts
  `{type, bbox:[xmin,ymin,xmax,ymax] normalized 0–1, angle: 0|90|180|270|None, content, merge_prev(text only)}`;
  `type` ∈ text/title/doc_title/paragraph_title/table/equation/image/chart/code/... `table` content is HTML,
  `equation` content is LaTeX, `image` content is None. `mineru_vl_utils.post_process.json2md(content_list)`.
  Serve with `vllm serve opendatalab/MinerU2.5-Pro-2605-1.2B --host 127.0.0.1 --port 8000
  --logits-processors mineru_vl_utils:MinerULogitsProcessor`.
  Sources: `mineru-vl-utils` README; wheel `mineru_vl_utils/structs.py`.

### Hardware + license
- Package license: `LicenseRef-MinerU-Open-Source-License` = Apache-2.0 + additional terms: separate
  commercial license required if MAU > 100M or monthly revenue > USD 20M; online services must attribute
  MinerU. Weights: **Pro = Apache-2.0**, 2.5 (2509) = **AGPL-3.0**.
- Base install runs on CPU (ONNX small models + llama.cpp Vulkan VLM); `mineru[full]` for NVIDIA.
  Docs: plan ≥16 GB system memory; NVIDIA 8 GB VRAM is a "planning starting point"; A100 recommended for
  throughput (2.12 fps, vllm-async). Sources: repo `LICENSE.md`; wheel METADATA; docs usage/tiers; HF cards.

---

## Flagged / unverified
1. `vllm serve PaddlePaddle/PaddleOCR-VL-1.6` is inferred from the 1.6 card's vLLM link, the vLLM
   architecture registry (`PaddleOCRVLForConditionalGeneration`) and 1.6's "architecture fully compatible
   with 1.5" claim; the recipe page still shows the v1 id. Exact served-model-name unverified.
2. No official minimum-VRAM figure for PaddleOCR-VL.
3. The question's artifact names `content_list.json` / `middle.json` are **legacy MinerU 2.x**; MinerU 4.0
   uses `middle_json.json` / `structured_content.json` / `markdown.md`.
4. "MinerU 2.5" is the *model* generation; the current *package* is 4.0 with a different CLI
   (`-b vlm-*` flags are 2.5.4-era).
5. Some MinerU `next/cli/*` pages are Chinese-only; the English summary above relies on their tables.
