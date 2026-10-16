# Is datatrove like Temporal or AWS Step Functions?

Short answer: **no — it is a different layer.** Temporal and Step Functions are *control plane*
orchestrators: they coordinate work that happens somewhere else. Datatrove is a *data plane* engine:
it owns the bytes and does the processing itself. The right mental siblings for datatrove are Apache
Beam, Spark, Ray Data and Dask — with a small amount of Airflow-style job dependency built in.

## What datatrove actually is

From its README and source (verified):

- 'A library to process, filter and deduplicate text data at a very large scale. It provides a set of
  prebuilt commonly used processing blocks with a framework to easily add custom functionality.'
- Pipelines are **platform-agnostic**, running 'out of the box locally or on a slurm cluster'.
  Executors shipped: LocalPipelineExecutor, SlurmPipelineExecutor, RayPipelineExecutor,
  JobsPipelineExecutor.
- Filesystems abstracted through fsspec, so local, S3 and friends look alike (the datafolder).
- The work unit is a **PipelineStep**: an abstract class whose run(data, rank, world_size) is a
  generator over a stream of Documents. Blocks declare dependencies, emit stats, and are chained into
  a linear pipeline.
- Concurrency is **data parallelism**: the same block sequence over N shards. LocalPipelineExecutor
  takes tasks (total shards), workers (how many run at once), local_tasks/local_rank_offset (which
  slice this machine runs).
- Resumability is **task-level and file-based**: skip_completed=True by default, with completion
  markers written under logging_dir; plus job dependencies via depends=, which waits with a poll
  loop ('Dependency job still has X/Y tasks. Waiting...').
- Error routing is a data-pipeline idiom: a failing record goes to an exclusion_writer instead of
  killing the job.

## Side by side

| Dimension | datatrove | Temporal | Step Functions |
|---|---|---|---|
| Category | data plane: batch processing engine | control plane: durable execution | control plane: managed state machine |
| Unit of work | a task = a shard of documents through a block chain | a workflow instance; its activities | an execution; a state |
| Where compute runs | in-process, on the machine/cluster that owns the data | in worker processes you run, told what to do by the server | in whatever the state invokes: Lambda, ECS, etc. |
| State | the document stream; persistence = files on disk plus completion markers | server-side event history; replay reconstructs workflow state (code must be deterministic) | JSON passed between states, capped at 256 KB |
| Resume after crash | re-run the job; completed *tasks* are skipped; a half-done task restarts | resume mid-workflow from history, instruction-level | resume from the last completed state transition |
| Retries | ad hoc per block or per call (e.g. HTTPFetchReader max_retries) | first-class: retry policies, backoff, heartbeats, timeouts | first-class: Retry/Catch per state |
| Control flow | essentially linear stages; multi-phase patterns by convention | arbitrary code: loops, conditionals, child workflows | explicit DSL: Choice, Parallel, Map, Wait |
| Waiting / long-lived | no notion of waiting for an external event | signals, timers, human approval, workflows lasting months or years | Wait states, task tokens; Standard up to 1 year, Express up to 5 minutes |
| Lifetime | a job that runs and exits | a durable entity | an execution |
| What it moves | the data itself | references/ids; data lives elsewhere by convention | small JSON; data lives elsewhere |
| Closest siblings | Beam, Spark, Ray Data, Dask, HF datasets processing | Cadence, Conductor; durable-execution engines | Airflow, Argo Workflows, Durable Functions, Dagster |

## Where the analogy holds

1. Ordered stages with names, retries and the ability to resume after failure.
2. Datatrove's executors make it *also* a scheduler (Slurm, Ray), and depends= gives a genuine
   mini-DAG between jobs.
3. Its multi-phase deduplication — ExactDedupSignature, then ExactFindDedups, then ExactDedupFilter
   (and the same shape for MinHash: signature, buckets, cluster, filter) — is a scatter-gather with a
   **barrier**. That is structurally the same thing as Step Functions' Map-then-Reduce or a Temporal
   workflow that fans out children and waits for them, except the intermediate state is files on S3
   rather than a service-held manifest.
4. The FinePDFs pipeline illustrates the idiom: nine stages, each reading and writing staged
   directories of jsonl/zstd, so a re-run re-reads artifacts and skips completed tasks. That is
   artifact-driven checkpointing, in the same spirit as a workflow's persisted state.

## Where the analogy breaks

1. **Granularity of resume.** Datatrove resumes at task boundaries by redoing the task; Temporal
   replays a deterministic workflow to rebuild in-flight state, so it resumes between individual
   operations.
2. **No durable execution semantics.** There is no event history, no deterministic-replay
   requirement, no signals, no timers, no exactly-once activity guarantees, no versioning of running
   workflows, no human-in-the-loop waits.
3. **It does the work.** Temporal and Step Functions *call out* to services; datatrove *is* the
   service. It invokes your Python functions on document batches rather than your microservices.
4. **Control flow.** Steps are a linear block chain, not a state machine with branching, parallel
   arms, compensation or escalation.
5. **Data volume.** Datatrove is built for terabytes of documents; Step Functions passes 256 KB of
   JSON between states and Temporal carries small payloads by design.

## How they compose rather than compete

A realistic production shape: **Temporal or Step Functions owns the process** — approvals, retries
across teams, notifications, month-long waits — and inside one activity or state it launches a
**datatrove or Spark job** for the heavy data work, then polls it. Airflow/Argo/Dagster play the same
middle role: a control plane that triggers datatrove runs on a schedule. Nobody would model
FinePDFs' 476M-document pass as a Step Functions state machine, and nobody would model a payment
approval flow as a datatrove pipeline.

## One-line answer

Datatrove is Beam/Spark-shaped (data plane, data-parallel tasks over sharded documents, file-based
resume), with a pinch of Airflow (job dependencies and completion markers). Temporal and Step
Functions are orchestration planes that coordinate work happening elsewhere. They are complementary
layers, not alternatives.
