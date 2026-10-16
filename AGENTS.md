# AGENTS.md

Pointers, not rules: where things live and the facts agents keep re-deriving.

- The spec is `docs/spec/finepdfs-integration.md`, the domain vocabulary is `docs/GLOSSARY.md`, and the
  decisions of record are `docs/adr/`. Read the glossary and every ADR a change touches before editing.
- The installed `.venv/bin/finepdfs` has a dangling shebang. Run the CLI as
  `PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli ...`.
- Checks: `.venv/bin/python -m pytest` (the default suite runs offline, no model, no network) and
  `ruff check .` (clean; `bench/` is excluded on purpose).
- Ticket slices are `.scratch/finepdfs-cocoindex/issues/`. The directory is gitignored, so it exists in
  no worktree: read it by absolute path, never relative.
- Machine state -- auth, a GPU, model paths -- is checked, never assumed; the rule is in
  `CODING_STANDARDS.md`.
- The review loop (`.github/workflows/review.yml`) needs the `OLLAMA_API_KEY` repository secret: two
  models review the diff, one per axis, and upsert one non-blocking comment. It is a comment, not a
  gate; with no secret it posts how to set one and exits 0.
