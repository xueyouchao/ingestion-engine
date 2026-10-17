# AGENTS.md

Pointers, not rules: where things live and the facts agents keep re-deriving.

- The spec is `docs/spec/finepdfs-integration.md`, the domain vocabulary is `docs/GLOSSARY.md`, and the
  decisions of record are `docs/adr/`. Read the glossary and every ADR a change touches before editing.
- The installed console script's shebang is worth checking before you run it:
  `head -1 .venv/bin/finepdfs` names the interpreter it was built against. If that path is not on
  this machine, run the CLI as `PYTHONPATH=src .venv/bin/python -m finepdfs.interfaces.cli ...`.
- Checks: `.venv/bin/python -m pytest` (the default suite runs offline, no model, no network) and
  `ruff check .` (must report no findings; `bench/` is excluded on purpose).
- Ticket slices are `.scratch/finepdfs-cocoindex/issues/`. The directory is gitignored, so it exists in
  no worktree: read it by absolute path, never relative.
- Machine state -- auth, a GPU, model paths -- is checked, never assumed; the rule is in
  `CODING_STANDARDS.md`.
