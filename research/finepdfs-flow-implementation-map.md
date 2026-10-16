# FinePDFs flowchart: box-by-box implementation map

*Question answered: are all the steps in the FinePDFs flowchart implemented using
github.com/huggingface/datatrove? Short answer: **no — every step is ORCHESTRATED by datatrove, but
roughly half the boxes are custom code** (subclasses of datatrove's PipelineStep) plus external
libraries. Verified by reading run_finepdfs_pipeline.py (785 lines) and the block sources.*

## The nine pipeline steps, in execution order

From main() at the end of run_finepdfs_pipeline.py (lines 770-779):

| # | Function | What it does | Flowchart boxes it covers |
|---|---|---|---|
| 1 | run_filter_pdfs_and_refetch | read the CommonCrawl index, keep PDFs, split truncated vs not, re-fetch, drop corrupt | source, >1MB / <1MB, truncated split, refetch, remove corrupted |
| 2 | run_content_dedup_ocr_organize | byte-level exact dedup, then the OCR-vs-not decision | exact dedup (bytes), Needs OCR? |
| 3 | run_extract | Docling for the No branch, RolmOCR via vLLM for the Yes branch | Docling / RolmOCR extraction |
| 4 | run_postprocess | the per-branch cleanup rules | removing boilerplate / page numbers / tables / image annots / repeated pages |
| 5 | run_language_filter | per-page language ID, then threshold-based language selection | Language Identification with GlotLID |
| 6 | run_exact_dedup | exact dedup on normalised text (whitespace removed), per language | Exact deduplication |
| 7 | run_model_classification | ModernBERT / mmBERT quality-and-language filtering, per chunk, on GPU | Model based filtering (English only) |
| 8 | run_minhash | MinHash near-duplicate clustering, per language | Near-duplicate removal (MinHash) |
| 9 | run_push_to_hub | package into HuggingFace datasets as parquet | FinePDFs |

Note the pipeline is **resumable at any stage** because each step reads and writes jsonl/zstd files
on disk — not one monolithic job. Constants at the top of the file wire the stage directories
together (SPLIT_TRUNCATION_DIR, DEDUP_OUTPUT_DIR, PER_LANGUAGE_DIR_EXACT, PER_LANGUAGE_DIR_MINHASH,
PUSH_INPUT_DIR). LIMIT is set to 10000 with the comment 'Adjust as needed, low for demo purposes
(for real run we didn't limit)'.

## Box by box: datatrove, custom, or external?

| Flowchart box | Implemented by | Detail |
|---|---|---|
| CommonCrawl PDFs 2013-2025 (source) | **datatrove readers**, plus custom adapter | ParquetReader over the CommonCrawl index for crawls >= CC-MAIN-2019-47; a custom WarcIndexReprocess for older crawls (the index parquet does not exist before then). Custom LambdaFilter(filter_non_pdf) and an index_adapter. |
| >1MB / <1MB split, Truncated / Non Truncated | **custom predicates + datatrove LambdaFilter** | Custom filter_non_truncated from blocks/utils.py; the truncated set gets an exclusion JsonlWriter. |
| Refetch from Internet | **datatrove** | datatrove.pipeline.media.readers.http_fetch.HTTPFetchReader(workers=15, max_retries=3, timeout=(60,60), download_timeout=600). A stock datatrove media reader. |
| Remove corrupted PDFs | **mixed** | datatrove MimeTypeFilter(mime_types=MIME_TYPES pdf) plus a custom LambdaFilter on media_bytes and custom blocks/corrupted_utils.py (check_is_corrupted_or_encrypted), which pymupdf-based checks use. |
| Saving the re-fetched PDFs | **datatrove** | media_writers.zstd.ZstdWriter (100 GB files). |
| Needs OCR? | **custom (a trained model)** | blocks/predictor/ocr_predictor.py, class PDFScannedPredictor, loading models/xgb_ocr_classifier/xgb_classifier.ubj with XGBClassifier. Features are extracted with pymupdf: **7 document-level features** (num_pages_successfully_sampled, num_unique_image_xrefs, num_junk_image_xrefs, garbled_text_ratio, is_form, creator_or_producer_is_known_scanner, class) and page-level features over 8 sampled pages. It emits ocr_prob and garbled_text_ratio; the pipeline's _filter_ocr routes to OCR at ocr_prob >= 0.2 or garbled_text_ratio > 0, with a comment pointing at the training notebook for the threshold. |
| RolmOCR extraction (Yes branch) | **datatrove runner + external model** | datatrove.pipeline.inference.run_inference (InferenceRunner, InferenceConfig, InferenceResult) driving vLLM (the README notes production used LMDeploy; the repo uses vLLM for supportability). |
| Docling extraction (No branch) | **custom block + vendored external library** | blocks/extractors/docling.py wraps the **vendored, modified Docling** in docling_code/ (docling, docling-core, docling-ibm-models plus custom_code), using the OpenVINO INT8-quantised layout model (models/heron) and PyMuPDF as backend. |
| Removing failed / corrupted / repeated pages (OCR branch) | **custom** | pipeline_utils/extract_utils.py: RepetitionChecker (repeated lines, sentences, characters with tunable thresholds) plus pipeline_utils/postprocess_utils.py helpers (DropFailedDocuments, CoallesceFailedPages). |
| Removing boilerplate, page numbers, corrupted tables, image annotations (Docling branch) | **custom PipelineStep subclasses** | postprocessing/boilerplate.py (TagBoilerplateFormatter, detect_boilerplate with min/max thresholds and a min-pages-with-pattern rule), postprocessing/page_numbers.py (PostprocessPageNumbers), postprocessing/tables.py (CleanTables), postprocessing/remove_image_annots.py (RemoveImageAnnotationsByRatio), postprocessing/normalize.py (Normalize: ftfy, weird spaces, OCR punctuation lines, split-word fixes). All are datatrove PipelineStep subclasses, all logic is theirs. |
| Language Identification with GlotLID | **custom wrapper + external library** | postprocessing/language.py LanguageTagger is a PipelineStep whose _requires_dependencies is fasttext (fasttext-numpy2-wheel); its backend argument is Literal['ft176', 'glotlid'], so it runs fasttext with either the fastText LID-176 model or **GlotLID**, per page, with averaging. datatrove does ship its own LanguageFilter, and tables.py imports it, but the per-page GlotLID behaviour used here is custom. |
| Language thresholds / language bucketing | **custom** | pipeline_utils/language.py SelectBestLanguage reads page_average_language_*_score metadata and applies thresholds from thresholds/th_values.json, picking the first language above its threshold, special-casing zxx_* scripts and emitting *_removed buckets otherwise. README: thresholds were tuned against google/gemma-3-27b-it labels on 20k samples per language, maximising F-beta with beta = 0.1. |
| Exact deduplication | **datatrove, twice, with custom content getters** | Before extraction on **PDF bytes** (ExactDedupConfig(content_getter=_get_media_bytes)) and again after extraction on **normalised text** (whitespace stripped) — both using datatrove's exact_dedup trio: ExactDedupSignature, ExactFindDedups, ExactDedupFilter. |
| Model based filtering (English only) | **custom model + datatrove inference runner** | Step 7 builds a rollout_model that sends every chunk through generate() via datatrove's InferenceRunner and parses comma-separated scores into per-field lists. The model is blocks/classification/multi_head_moder_bert.py: a custom MultiHeadModernBert that shares the common ModernBERT/ModernBERT-base layers across several sequence-classification heads to score multiple signals in one pass (with tf_batching.py for throughput). Tokenisation for chunking uses answerdotai/ModernBERT-large for eng_Latn and mmbert-colab/mmBERT-base otherwise, via the custom AddTextChunks (classification/label_utils.py). |
| Near-duplicate removal (MinHash) | **datatrove** | datatrove.pipeline.dedup MinhashDedupSignature, MinhashDedupBuckets, MinhashDedupCluster, MinhashDedupFilter with MinhashConfig and HashConfig. |
| Packaging into FinePDFs | **datatrove + custom adapter** | HuggingFaceDatasetWriter with a custom push_adapter (pipeline_utils/push_utils.py), writing parquet shards per language, plus a secondary filtered subset (fw_edu_scores >= 0.5) pushed as finepdfs_fw_edu_subset. TokensCounter from datatrove.pipeline.tokens is available for accounting. |

## What datatrove actually provides here

- **The execution model**: LocalPipelineExecutor (and SlurmPipelineExecutor is imported in tables.py),
  the PipelineStep base class every custom block subclasses, the Document data model, ranks/tasks
  sharding, dependency management (_requires_dependencies), and the datafolder/IO abstraction
  (get_datafolder, s3 paths).
- **The blocks used as-is**: WarcReaderFast, ZstdReader, ZstdWriter, HTTPFetchReader, MimeTypeFilter,
  JsonlReader, ParquetReader, JsonlWriter, HuggingFaceDatasetWriter, LambdaFilter, TokensCounter,
  the exact-dedup trio, the MinHash quartet, and the inference runner.
- **A whole media/ subpackage** in datatrove (media/media_readers, media/media_writers,
  media/filters, media/readers plus datatrove.utils.media.iter_pages) — i.e. HuggingFace extended
  datatrove itself for PDF/media work, probably extracted from this project. The same is true of
  datatrove.pipeline.inference (the vLLM/LMDeploy runner). So the boundary between 'datatrove' and
  'FinePDFs code' is not a clean public-library line: parts of datatrove exist because of this work.

## What is NOT datatrove

The domain logic, which is the majority of the intellectual work in the flowchart:

1. the **XGBoost OCR router** (trained on 1,350 hand-annotated PDFs, released as HuggingFaceFW/ocr-annotations);
2. **Docling integration**, vendored with modifications and an OpenVINO-quantised layout model;
3. the six **postprocessing rules** (boilerplate, page numbers, tables, image annotations, page-repetition, normalisation);
4. **GlotLID per-page tagging plus the threshold table** tuned against an LLM judge;
5. the **MultiHeadModernBert quality classifier**;
6. the **truncation split, corruption checks, and the stage wiring** in the pipeline script.

## Answer in one line

**datatrove is the skeleton and the plumbing — executor, block protocol, readers, writers, dedup,
inference runner, token counter, and a media/ subpackage it grew for this job. FinePDFs' own code is
the flesh: OCR routing, extraction configuration, postprocessing rules, language thresholds, and the
quality classifier.** Every box in the picture is a datatrove pipeline step; only about half the
boxes are datatrove's own code.
