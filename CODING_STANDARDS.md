# Coding standards

The judgement calls a reviewer enforces. Anything a machine can decide lives in the linter, not here:
`ruff check .` is the mechanical gate, and this file holds only what it cannot read. Each rule names the
defect that produced it, so the reason outlives the people who were there.

## A port's docstring is true for every adapter

A docstring on a port in `src/finepdfs/domain/ports.py` promises what the domain needs from any adapter,
so it may name only that. Vendor- and model-specific translation belongs in the adapter, beside the thing
that needs translating, never at the caller. The `LanguageIdentifier` port was documented as returning
ISO 639-3 codes; a labeller returned the model's own two-letter labels and the pipeline wrote them into
published records. When an adapter cannot honour the port's words, either the port is wrong and every
adapter changes, or the adapter keeps its own promise in its own docstring. See ADR-0001.

## An error names the failure that occurred

A raised error says what actually went wrong, not which branch noticed it. A missing model file raised
"unknown language mode", which sent the reader to the wrong place: the mode was known, the file was not.
Name the missing thing, the rejected value or the failed condition, and keep the unknown-mode error for
an actually unknown mode (`src/finepdfs/infrastructure/language/registry.py` is the shape).

## One concept has one source of truth

A decision lives in one place and every consumer reads that place. The corpus-wide duplicate decision
was written twice, once in `publish_corpus` (`src/finepdfs/infrastructure/cocoindex/flow.py`) and once in
the CLI's dry run (`src/finepdfs/interfaces/cli.py`), so the two could disagree and the dry run -- the
tool an operator uses to size a GPU job -- could lie. ADR-0006 makes the deciding pass a single object
that both consume. If two callers need the same answer, factor the answer.

## Docs say how to check machine state; they never assert it

A document records the command that reveals auth, a GPU or a model path, and what the answer means. It
never states the answer, because the answer changes without the document. The spec asserted the GitHub
CLI was unauthenticated; it was authenticated, and agents spent real time on a phantom. The rule is the
shape, not the fact: name the check (`gh auth status`) and what each answer means, and let the reader run
it. The spec's own sentence is being corrected to that shape on a branch still in flight, so this rule
does not cite the spec as already fixed.

## A published record must be reproducible

What lands in a record or a memo key names something stable -- a digest, a model name, a tool version --
never something ephemeral like a rented host's address, a lease id or a temp path. ADR-0005 keys
extraction on engine identity: a host address there would re-extract the whole corpus every run, and
`DocumentRecord.engine` (`src/finepdfs/domain/publishing.py`) would record an address that means nothing
later. ADR-0002 makes the corpus reproducible from the manifest for the same reason.
