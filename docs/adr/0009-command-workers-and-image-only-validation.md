# 9. Rent GPU workers through the command provider, and validate them on image-only PDFs

Date: 2026-10-18

## Status

Accepted. This fixes the worker shape and the acceptance gate before implementation; the worker itself
is still to be built.

## Context

ADR-0007 already put the rented GPU behind a provider port. The seam exists and is tested: `none`,
`static` and `command` providers, a `GpuEndpoint` that separates the address from the memo identity, and
a lease that wraps the whole CocoIndex update. What does not exist is a real worker to put behind it.

The deferred worker ticket asks for a Modal app and a RunPod app that serve an OCR engine and print
their endpoint as one JSON object, wired through `FINEPDFS_GPU_START_COMMAND` /
`FINEPDFS_GPU_STOP_COMMAND`, with a stable model-and-version identity and a released lease. It is blocked
on a GPU account and on choosing which engine to serve.

Three implementation shapes are available:

* a native SDK adapter implementing `GpuProvider`,
* a REST client living in the core package,
* a start/stop script invoked by the existing `CommandGpuProvider`.

The first two put vendor lifecycle code and dependencies inside `src/` before either vendor has run.
The third uses the seam that already exists: the provider runs a command, the command rents a worker,
and the pipeline never imports Modal or RunPod.

This host has no GPU. A local-first, fully offline worker rehearsal was considered, but it is not a
prerequisite for the first real run: the useful parts — the worker image, the endpoint contract and the
failure tests — can be delivered inside the Modal phase. What cannot be skipped is real evidence on
image-only PDFs. A text-layer PDF never exercises the OCR engine, and a stub server never proves that a
rented worker starts, serves, and is given back.

## Decision

### The worker is a command script, with the vendor SDK or REST inside it

The integration boundary stays `CommandGpuProvider`. A Python `start` script creates one worker, saves
the vendor lease ID in a state file immediately, then waits for readiness, prints exactly one JSON
object, and exits. Saving the ID before the readiness wait is deliberate: if readiness fails or the start
command times out, `CommandGpuProvider` still runs the stop command, and stop needs an ID to terminate a
worker that may already be billing. A Python `stop` script reads that state and terminates the worker;
it is safe to run twice, and it must tolerate a partially started worker whose endpoint never became
ready.

The vendor-specific implementation lives outside the package:

```text
deploy/
  gpu/          shared image, model server, lease-state format
  modal/        Modal SDK start/stop
  runpod/       RunPod REST start/stop (later)
```

Modal is implemented with its Python SDK because the SDK covers sandbox/app creation, GPU selection,
tunnel discovery and termination in one place. RunPod is implemented with its REST API, or its official
SDK if that covers the same lifecycle; its pod create/status/delete path is simple enough that a new core
dependency is not justified. A vendor CLI may be wrapped by a script, but a pure shell pipeline is not
the contract: readiness polling, JSON parsing, timeouts, lease state and idempotent teardown need real
error handling.

The start command must detach. `CommandGpuProvider` runs it synchronously and waits for it to exit before
using the endpoint; a start command that stays in the foreground would deadlock the lease.

The JSON contract is:

```json
{"base_url":"https://worker.example/v1","model":"PaddleOCR-VL-1.6","detail":"paddleocr-vl@1.6:weights-<digest>:image-<digest>"}
```

`base_url` is the ephemeral OpenAI-compatible base URL and **must include `/v1`**. `ChatEndpoint` appends
only `/chat/completions`, so returning a bare host would send OCR requests to the wrong route; the worker
must serve both `/v1/chat/completions` and `/v1/models`. `detail` is the deployment half of the Engine
Identity and must name the model and its version — weights and/or image digest — and never the URL.
Credentials come from the environment and are never committed. The start script writes nothing secret to
stdout or logs.

No new port is introduced. `GpuProvider` remains the port and `CommandGpuProvider` remains the adapter.
If a native adapter is later justified, it is one entry in the existing `PROVIDERS` registration point,
not a second lifecycle abstraction.

### Engine and provider order

PaddleOCR-VL is the first engine. It is Apache-2.0, its OpenAI-compatible `/v1` surface is already
implemented by `PaddleOcrVlEngine`, and its page DPI is already part of the Engine Identity. MinerU
remains supported by its existing `/file_parse` adapter, but it is AGPL and a harder service to package;
it is not the first worker.

Modal is the first provider, because its SDK gives the cleanest per-run sandbox/tunnel/terminate path.
RunPod follows through the same start/stop contract. The first implementation does not add a native
Modal or RunPod adapter: the command path must prove itself before a vendor dependency is earned.

### Validation is a gate, not a smoke test

The acceptance evidence is ordered from cheapest to most expensive:

| Gate | Evidence | What it proves |
|---|---|---|
| Contract | Fake start/stop commands; one JSON object; start detaches; ID saved before readiness; stop runs on success, readiness failure and failure | The lease and provider contract, without a GPU |
| Worker | Modal health/`/v1/models`; one real image-only page | The worker actually serves PaddleOCR-VL |
| Page mapping | The genuine 1-page and 2-page AOML scans | OCR runs, pages survive, original numbering is preserved |
| Incremental run | Full pinned NOAA corpus cold, then the same command with a new lease | The second run makes 0 extraction calls and reuses everything |
| Teardown | Start failure, readiness failure after creation, run failure, stop failure; provider console/API afterwards | No worker is left billing |
| Downstream | Index and read/search an OCR page | Accepted OCR text is usable evidence, not just an extraction result |

The two genuine scans are the minimum real-OCR preflight, not the whole validation:

* the 1-page scan proves a single image-only page produces non-empty accepted text;
* the 2-page scan proves page 1 and page 2 stay distinct, with the form header on page 2 and no collapse
  onto page 1.

PaddleOCR-VL is a different engine from Docling. Its output must be recorded on its own terms; the old
Docling character counts are not acceptance criteria for this engine.

The full-corpus run is the actual incremental gate. It must show the scans routed to OCR, native
documents staying native, a cold run with real extraction calls, and a warm run with zero calls and an
unchanged Engine Identity. The identity must contain no Modal URL.

Failure is part of the feature: a start that cannot rent a worker exits as an operator problem, a run
that loses its worker still releases the lease, and a stop that fails after an otherwise good run is
reported and exits nonzero because the worker may still be billing.

The local-first Phase 1 is not mandatory. Its worker image, endpoint contract and fake-command tests are
folded into the Modal phase; the first real environment is a one-page Modal smoke, followed by the scans
and the corpus. This trades cheap local iteration for speed, and is mitigated by contract tests before
the first paid run, a one-page first run, a short lease timeout, and checking the provider console after
each run.

## Consequences

* The base install stays model-free and vendor-free. Modal and RunPod dependencies live in the deploy
  environment; nothing in `domain/`, `application/`, the flow or the engines imports either vendor.
* RunPod later is either a second pair of start/stop scripts implementing the same stdout contract, or,
  only if the command path cannot express something it needs, one registered `GpuProvider` adapter. In
  both cases the pipeline does not change.
* The start script is testable with a fake runner, but real readiness and teardown remain provider
  behaviour: they must be checked against the provider, not inferred from exit codes.
* A warm rerun still leases a worker even when the memo makes zero extraction calls. That is accepted
  for this gate; avoiding the lease when no OCR is needed would be a separate optimisation.
* The validation corpus has only two genuine image-only scans. The evidence proves the worker, routing,
  page mapping and incrementality, not OCR quality across languages or layouts.
* A native SDK adapter remains a possible follow-up, not a prerequisite. The decision is deferred until
  the command path has a real successful run.

## Alternatives considered

* **Native Modal/RunPod adapter first.** Rejected: it couples the core to a vendor and a dependency
  before the command path has run, and makes the second provider a separate code path. The command
  boundary already exists and is tested.
* **REST client inside `src/`.** Rejected for the same coupling reason, plus manual auth and polling
  logic in the package that has no business owning a vendor lifecycle.
* **Pure shell/CLI scripts.** Rejected as the contract: shell is fragile for readiness, JSON, timeout,
  lease state and idempotent stop. A Python script may call a vendor CLI internally.
* **Require the offline Phase 1 first.** Rejected as a gate: the useful contract and failure tests move
  into the Modal phase, and the first real run is a one-page smoke rather than a full corpus.
* **RunPod first.** Rejected: Modal’s SDK gives the cleaner sandbox/tunnel/terminate path for a first
  per-run lease; RunPod follows the proven contract.
* **MinerU first.** Rejected: AGPL and a harder service to package; PaddleOCR-VL’s OpenAI-compatible
  surface and existing adapter are the lower-risk first worker.
* **Run the whole corpus through OCR for quality evidence.** Rejected: the router is part of the feature,
  only image-only Documents need OCR, and forcing every page through a GPU would be expensive and
  unrepresentative.
* **Treat two image-only PDFs as the acceptance test.** Rejected: they do not prove incremental reuse,
  stable identity, or lease release. They are the preflight, not the gate.

## References

* [ADR-0005: Memoise the engine call only](0005-memoise-the-engine-call-only.md)
* [ADR-0007: Rent the GPU behind a provider](0007-rent-the-gpu-behind-a-provider.md)
* [FinePDFs integration spec](../spec/finepdfs-integration.md)
* [Glossary](../GLOSSARY.md)
