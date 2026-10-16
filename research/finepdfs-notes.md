# FinePDFs (HuggingFaceFW/finepdfs) — verified facts, and what it changes

*Answers: does finepdfs offer a more complex way to ingest PDFs? Verified against the dataset card
and the HF datasets-server API on this machine (not secondhand).*

## What it actually is

- 106 CommonCrawl dumps; PDFs crawled summer 2013 to February 2025. Static artifact — not a live feed.
- **476,178,356 rows** (~476M documents), **~5.4 TB of parquet** (~11.5 TB uncompressed in memory).
- License ODC-By. Download via datatrove / huggingface_hub / datasets, with per-language subsets.
- Fields: text, id (urn:uuid), dump, url (original PDF location), date, file_path (S3 WARC path),
  offset, language, per_page_languages, page_average_lid(+score), full_doc_lid(+score),
  is_truncated, **processor** (docling | rolmOCR), **page_ends** (end index of each page),
  token_count (gpt2 tokenizer).
- Already applied for you: truncation identification + hydration, **OCR detection and extraction**,
  text postprocessing, language ID, **exact dedup + MinHash dedup**, filtering, **PII anonymization**.

## The key fact: there are no original PDFs in it

Only extracted text. The processor field says which extractor produced it (docling or rolmOCR), and
url + WARC path/offset point back at the original bytes in Common Crawl if you ever want them
(requester-pays S3).

## So — a more complex way to ingest PDFs? No: the opposite, with a twist

- **Per-document work is simpler, not more complex.** Parsing — the expensive stage — is already
  done (and OCR'd, deduped, PII-scrubbed). You start at chunk + embed.
- **You cannot use it to benchmark PDF parsing** (see bench/README.md): there is nothing to parse.
  To measure parse throughput, you need raw PDFs (arXiv S3, govinfo, WARC extraction, your own corpus).
- **The complexity did not disappear — it moved.** 476M rows / 5.4 TB of parquet is now a
  data-engineering problem: shard streaming, language subsets, storage, and the embedding bill.
  The pipeline is no longer parse-bound; it is embed/IO-bound.
- **What you give up:** re-parsing with your own parser settings (no layout, no images, no tables
  fidelity), their anonymization choices instead of your scrubbing policy, and freshness frozen at
  Feb 2025. If you need the raw bytes later, it is a WARC re-hydration exercise, not a re-parse.

## Mapping onto the two-stage design (research/pdf-pipeline-design.md)

| Design-doc step | FinePDFs equivalent |
|---|---|
| Stage A: parse PDF to text | done (docling or rolmOCR; which one is in processor) |
| sha256 dedup | exact + MinHash dedup already applied |
| PII scrub before indexing | PII anonymized upstream |
| Language tagging | language + per-page LID fields |
| Page-level lineage | page_ends gives page boundaries in the text (better than char offsets) |
| Stage B: chunk + embed + store | **yours to do — this is the whole job here** |

## Wiring it into CocoIndex

No built-in HF/parquet connector, but sources are plain async generators (the HackerNews example
pattern) and local files are a source. The practical shape is **one component per parquet shard** —
resume granularity lands at shard level, a shard that did not change is skipped entirely:

~~~python
# 1) download or mount the language subset you want (start small: one language, even one dump)
# 2) one component per parquet file; inside it, declare one row per document chunk

@coco.fn(memo=True)
async def ingest_shard(shard: FileLike, table: postgres.TableTarget[ChunkRow]) -> None:
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(shard.file_path.resolve())
    embedder = coco.use_context(EMBEDDER)
    id_gen = IdGenerator()
    for batch in pf.iter_batches(batch_size=256, columns=['id', 'text', 'url', 'date',
                                                          'language', 'page_ends', 'processor']):
        for d in batch.to_pylist():
            chunks = _splitter.split(d['text'], chunk_size=2000, chunk_overlap=500,
                                     language='markdown')
            for chunk in chunks:
                table.declare_row(row=ChunkRow(
                    id=await id_gen.next_id(chunk.text),
                    source=d['id'], url=d['url'], date=d['date'],
                    language=d['language'], processor=d['processor'],
                    text=chunk.text,
                    chunk_start=chunk.start.char_offset, chunk_end=chunk.end.char_offset,
                    embedding=await embedder.embed(chunk.text),
                ))

@coco.fn
async def app_main(sourcedir: pathlib.Path) -> None:
    shards = localfs.walk_dir(sourcedir, recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=['**/*.parquet']))
    table = await postgres.mount_table_target(PG_DB, table_name='pdf_chunks',
        table_schema=await postgres.TableSchema.from_class(ChunkRow, primary_key=['id']))
    table.declare_vector_index(column='embedding')
    await coco.mount_each(ingest_shard, shards.items(), table)
~~~

Notes:
- memo=True on the shard-level fn = an unchanged shard costs nothing on rerun; edit one shard and
  only that shard re-embeds.
- The row id (urn:uuid) is a stable component/row key; page_ends lets you cite pages, not just chars.
- Scale-out: same rule as the design doc — partition shards across processes, one LMDB each
  (CocoIndex state is per-process).
- Budget the embedding step first: this corpus shifts the bottleneck from parse (my bench: 632
  pages/s) to your embedder (chunks/s x 476M docs). Start with one language subset and measure.

## Verdict

FinePDFs is not a more complex *ingestion* mechanism — it is ingestion with the hard part already
done, at the cost of re-parse control and freshness. Use it when you want scale and text quality
(OCR'd, deduped, PII-safe) more than you want layout fidelity or your own parser. Use raw PDFs
(arXiv S3, govinfo, WARC, your own lake) when you are testing parsing itself, need page images or
tables, or need a live feed.

---

## Addendum: is the FinePDFs flowchart 'more complex' than a CocoIndex flow?

Short answer: **yes, it is a more complex *recipe* — but comparing it to 'a CocoIndex flow' is
comparing a recipe to a kitchen.**

- The FinePDFs diagram is one specific **data-cleaning recipe** for one specific artifact (a frozen
  pretraining corpus, built once with HuggingFace datatrove over 106 CommonCrawl dumps).
- CocoIndex is a **framework you write recipes in**. It supplies the machinery around your steps
  (parallelism, change detection, memoization, resume, target sync, lineage); it supplies none of
  the PDF-cleaning domain logic.
- So the ~10-line pipeline from the README (and from bench/ingest.py) is a *demo*, not a corpus
  builder. Add FinePDFs-grade cleaning to a CocoIndex pipeline and you would write roughly the same
  amount of domain logic. What you would gain is that re-running it costs the delta.

### Box-by-box: how each step would fit CocoIndex

| FinePDFs step | CocoIndex fit |
|---|---|
| PDF truncation identification (size/flag) | Easy — per-document logic in a memoized fn |
| Refetch truncated PDFs from the Internet | Awkward — a side effect that produces a *new input*; model it as a separate pre-stage that materializes PDFs into the source bucket |
| Remove corrupted PDFs | Easy — try/except + quarantine table (advanced_topics/exception_handlers) |
| 'Needs OCR?' branch | Easy — per-doc branch inside a fn |
| rolmOCR / docling extraction | Easy — and this is the expensive box, so memo=True is the whole point (the repo's pdf_embedding example uses docling under runner=coco.GPU) |
| Cleanup: boilerplate, page numbers, repetition, corrupted tables, image annotations | Easy — cheap text post-processing fns, memoized |
| Language identification (GlotLID) | Easy but DIY — CocoIndex has no built-in natural-language LID (ops/text has detect_code_language, which is for source code); wrap a LID library in a memoized fn |
| Model-based filtering (English only) | Easy — a memoized, batched classifier/LLM call |
| Exact deduplication | **Belongs outside at this scale** — corpus-global. CocoIndex reconciles per item, locally; a global reduce is expressible (the single fan-in 'graph-building pass' in examples/docs_to_knowledge_graph) but impractical while holding 476M docs |
| Near-duplicate removal (MinHash) | **Belongs outside** — same reason; global pairwise comparison is a Spark/datasketch-shaped job, not a per-item reconciliation |

### The two real mismatches

1. **Global operations.** Exact dedup and MinHash near-dedup are corpus-global. CocoIndex's model is
   per-item local reconciliation with a target store; there is no shuffle/join across items by
   design. For 476M documents, run those stages in a batch engine and feed the survivors to
   CocoIndex (or to your index directly).
2. **Feedback loops.** 'Refetch from the Internet' is a stage that creates new inputs. CocoIndex is
   source -> transform -> target, not a control loop. Model the refetch as an upstream job whose
   output is the source bucket.

### What CocoIndex would actually buy you if you built that flow on it

- The most expensive box (OCR/extraction) is fingerprinted by content, so tuning a *downstream*
  filter does not re-OCR the corpus — a batch pipeline re-runs stages from the top.
- Per-page lineage (FinePDFs gives you page_ends for this) so any cleaned chunk traces to a source
  page, which matters for legal/educational corpora.
- Crash resume without a hand-written checkpoint scheme.
- Quarantine instead of 'one bad PDF kills the shard'.
- The artifact stays *fresh* as new dumps land, instead of being frozen at Feb 2025 — that is the
  property their recipe deliberately does not need and CocoIndex exists to provide.

### The right comparison axis

It is not 'complex vs simple'. It is **batch corpus construction vs incremental index maintenance**:

- datatrove / Spark / Hadoop: one (or few) perfect passes over a whole corpus, global operations,
  frozen outputs. Correct tool for building FinePDFs itself.
- CocoIndex: continuous, per-item, delta-only maintenance of a live index. Correct tool for serving
  and updating a corpus that keeps changing.

In practice, they compose: build a clean corpus once with the batch tool, then keep the searchable
index over it fresh with CocoIndex as new documents arrive.

---

## Addendum: is the FinePDFs pipeline code open source? Yes — with one licensing catch

*Verified via the GitHub API and raw file fetches on this machine.*

**Repo: github.com/huggingface/finepdfs** — 'Codebase for FinePDFs'. 191 stars, 28 forks, created
2025-10-17, last pushed 2026-01-09. The dataset card links it as 'Reproduction code'. Its README:
'contains the end-to-end code to filter, extract, OCR, postprocess, deduplicate, classify, and
package large-scale PDF text data.'

### The licensing catch

- **Pipeline code: AGPL-3.0** (the LICENSE file is GNU Affero GPL v3; GitHub reports NOASSERTION).
- **Dataset: ODC-By 1.0** (permissive with attribution; also subject to CommonCrawl's terms).
- **Annotation dataset: ODC-By** (HuggingFaceFW/ocr-annotations, verified live).

So the data is easy to use, but the *code* is strong copyleft with the network clause: pulling it
into a service you expose to users carries real obligations. Reading the paper/README and
re-implementing the ideas is a different act from copying the code. Worth deciding deliberately
rather than by accident.

### What is actually in the repo

| Path | Corresponds to flowchart box |
|---|---|
| run_finepdfs_pipeline.py | the whole pipeline (datatrove blocks); quickstart: --crawl-ids CC-MAIN-2019-43 --languages eng_Latn |
| blocks/ | datatrove blocks: readers, extractors, predictor, classification, corrupted_utils.py, loggers.py |
| docling_code/ | vendored docling, docling-core, docling-ibm-models, plus custom_code |
| models/xgb_ocr_classifier/ | the trained OCR-routing classifier (cheap CPU text path vs expensive GPU OCR path) |
| models/model_prep_code/ | classifier training notebook |
| models/heron/ | OpenVINO INT8 quantization of ds4sd/docling-layout-heron |
| postprocessing/ | boilerplate.py, language.py, normalize.py, page_numbers.py, remove_image_annots.py, tables.py |
| classification/ | model-based filtering: teacher labeling, BERT training (edu_langs.txt, train_bert.py) |
| thresholds/ | language filtering thresholds (th_values.json, gemma_classify.py, find_th.py) |
| ablations/ | their experiments |

Stack requirements: uv, Python 3.12, torch 2.6.0, vllm>=0.8.5.post1, pymupdf==1.26.1, and a GPU for
the vLLM steps. THIRD_PARTY_NOTICES.md exists because Docling is vendored.

### Two details worth stealing (they are the real cost engineering)

1. **OCR-vs-no-OCR is a trained classifier, not a heuristic.** They manually annotated 1,350 PDFs,
   trained XGBoost on 7 document-level plus 120 page-level features, and ship both the model and the
   annotations. That is what lets a cheap CPU text extraction handle most documents and reserve GPU
   OCR for the ones that need it.
2. **Language thresholds were tuned against an LLM judge.** They used google/gemma-3-27b-it on a
   20k-per-language subsample and picked thresholds maximizing F-beta with beta = 0.1 (precision
   heavy). The chosen values ship as th_values.json.

### Honest caveats from the authors themselves

- 'The pipeline is a rough reproduction of our production pipeline. Some parts are optimized for
  clarity rather than optimal resource allocation or speed.'
- Production used LMDeploy; the repo uses vLLM (they recommend LMDeploy for production).
- The OpenVINO/heron quantization is 'a reference implementation rather than fully runnable code' —
  they do not ship the source PDFs/images.
- Minor inconsistency in their own docs: the dataset card says both 105 and 106 CommonCrawl snapshots.
- A dedicated blog post walking through the journey is still planned (space: HuggingFaceFW/FinePDFsBlog).

### What this changes for a CocoIndex port

Good news first: the domain steps are no longer a hand-wavy 'you write those yourself' — they are
concrete, readable code you can study (and, license permitting, lift as ordinary functions). The
postprocessing rules, the classifier-based routing, and the language thresholds are exactly the kind
of per-document logic that slots into a memoized per-file function.

Two things are unchanged from the earlier analysis:

- Global stages (exact dedup, MinHash near-dedup) still belong in a batch engine, not in per-item
  reconciliation.
- The batch-to-incremental translation is real work: their blocks assume a datatrove pipeline over a
  whole crawl, not per-document reconciliation against a target store.

And one thing is new: check the AGPL question before copying code, and budget for the GPU stack
(vLLM/LMDeploy plus an OCR model) if you want the image-based path at all.
