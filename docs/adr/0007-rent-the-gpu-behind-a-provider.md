# 7. Rent the GPU behind a provider, and memoise on a deployment identity, not on an address

Date: 2026-10-16

## Status

Accepted

## Context

The expensive engines are remote HTTP model servers: PaddleOCR-VL over an OpenAI-compatible `/v1`, and
MinerU over its own `/file_parse`. They were reached at a `--base-url` an operator supplied by hand,
which is right for a server that stays where it is and impossible for a GPU rented per run. This machine
has no GPU, so the intended shape is: rent one when an engine needs it, on Modal or RunPod, and give it
back afterwards.

That collides with the memo. ADR-0005 makes the memo key `(content digest, engine name, engine
identity)`, and the identity was `f"{model}@{base_url}"`. A rented worker answers on a new hostname every
run, so the key would change every run and the whole corpus would be read again -- which is exactly the
cost the memo exists to remove. The engine would be correct and the incrementality, the thing this
pipeline is for, would be gone.

One more constraint shapes the answer: the flow is configured by the environment alone, because
`cocoindex update main.py` takes no arguments of its own.

## Decision

A GPU is *leased*, and its address is kept out of the memo key.

* **A provider port.** `infrastructure/compute/` holds `GpuProvider` with one method,
  `lease() -> context manager[GpuEndpoint]`. Three adapters ship: `none` (the offline default),
  `static` (a server that already exists) and `command` (a command that rents one and a command that
  gives it back). Modal and RunPod are a command line today and an adapter behind the same seam later;
  no engine code knows about either.
* **Address and detail are separate fields.** `GpuEndpoint` carries `base_url`/`model`/`api_key`, which
  is where the bytes go, and `detail`, the deployment half of the Engine Identity the memo remembers. A
  provider that hands out a moving address must report a stable detail; the command provider derives one
  from its command line and model when the command does not name one.
* **`StableIdentity` decorates the engine.** `build_engine` wraps any engine in it when a detail is
  configured, so the engine keeps its name and its extraction and only the detail changes.
* **The lease wraps the run.** It is taken in the flow's `coco_lifespan`, around the `yield`, so a
  worker lives exactly as long as the run and is released even when the run fails. `dry-run` does not go
  through the flow, so it leases for itself.
* **The lease reaches the flow through the environment.** `export_endpoint` writes `FINEPDFS_ENGINE_*`,
  which is the only channel `cocoindex update main.py` has. A leased address overwrites the environment,
  because the run asked for that worker; a detail already set is kept, so a user can pin the memo name
  with `--engine-detail` and have it outlive the provider's derived default.

## Consequences

* A rented worker is a drop-in: the endpoint moves, the identity does not, and nothing is re-read.
* The detail is now an explicit, nameable thing rather than an accident of the transport. It is the
  operator's job to encode the model version in it: a worker whose *weights* change while the command
  line does not is, to the memo, the same worker.
* Two concurrent runs each lease their own worker; they cannot share one. That matches the state
  database, which already refuses a second writer.
* The lease releases on the way out -- on the success path and on the failure path alike -- because a
  leaked GPU bills by the hour.
* A start command that fails may already have launched a worker -- it exits nonzero after starting one, or
  prints no endpoint -- so the stop command runs before that failure is reported, exactly as it does after
  a failed run. A stop that fails at that point is noted on the start error rather than replacing it,
  because the start is the failure the operator has to act on. A command that could not even be parsed
  never ran, so there is nothing to give back and no stop is attempted.
* The stop command's result is checked, not discarded. A stop that fails while the run is already
  failing is reported beside the run's own exception -- logged, and attached to its traceback -- and
  that exception is then re-raised untouched, because the error that ended the run is the one the
  operator has to act on. A stop that fails after a run that otherwise finished is a provider failure
  the CLI reports and exits 2 on, exactly as a start that cannot run is.
* A provider that cannot start the run -- or whose command cannot even be parsed -- is an operator
  problem, so the CLI reports it and exits 2 rather than printing a traceback -- the same way an empty
  corpus is refused.

## Alternatives considered

* **Keep `model@base_url` as the identity.** Correct for a fixed server, catastrophic for a rented one:
  it makes every run a cold start.
* **Drop the engine identity from the memo key.** Then a model or engine change would serve text the
  new engine never produced, which is the failure ADR-0005 exists to prevent.
* **Add Modal and RunPod SDK adapters now.** Neither SDK is a dependency and neither has been run here.
  A provider is a command line until there is a reason it cannot be, so the layer ships with the seam
  and no vendor dependency.
* **Lease inside each engine.** Lifecycle would be per document instead of per run: a worker started and
  stopped per document, or leaked until the process ends.
