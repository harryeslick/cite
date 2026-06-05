# Agent guide for `cite`

`cite` is a deterministic citation manager you drive from the shell. You supply
judgement; the tool supplies reproducible bookkeeping.

Invoke it as `cite ...` when installed globally (`uv tool install`), or
`uv run cite ...` from this repo in dev — the commands below use `uv run`; drop
that prefix when `cite` is on your PATH.

**Load the contract first:** `uv run cite guide --json` returns the command list,
the 7 allowed citation types, and the required fields for each. Every command
prints one JSON object with a `status` field — read it and follow
`suggested_next` / `hint`.

## Workflow to add a file
1. `uv run cite peek <file>` — get a DOI / title / author guess deterministically.
2. `uv run cite search --doi <doi>` (or `--title "<t>" --author <a> --year <y>`).
3. Pick the best `.candidates` entry, then:
   `echo '<candidate-json>' | uv run cite add <file> --csl -`.
4. No DB match? Choose the right `--type` and add manually, supplying fields:
   `uv run cite add <file> --manual --type <cite_type> --field title=... --field author="Smith, Jane; Lee, Kim" --field issued=2023`.
   On `status: missing_fields`, ask the **user** for the listed `missing` fields,
   confirm, then re-run. Never fabricate bibliographic facts.

## Rules
- Citation types are limited to: `journal-article`, `book`, `book-section`,
  `web-site`, `trial-report`, `data-set`, `other-report`.
- `status: duplicate` means identical file bytes are already filed — stop.
- `author` field format: `Family, Given; ...`; a comma-less entry is a literal
  (organisational) name. `issued`/`accessed`: `YYYY[-MM[-DD]]`.
- Library path: `--library`, else `$CITE_LIBRARY`, else `./library`.
- Each reference is a self-contained bundle dir `<id>/` (record + original file +
  any extracted markdown). `cite remove <id>` deletes the whole bundle.

## Optional: full-text markdown
- `cite extract <id>` converts a stored reference to full markdown locally (Docling
  VLM); output goes to `<id>/<id>.md` (+ `<id>_artifacts/`). Needs the `extract`
  extra — on `status: error` with an install hint, tell the user to install
  `cite[extract]`; never treat its absence as a failure of the core workflow.
- `cite text <id> [--path-only]` prints the extracted markdown (or its path).

See `README.md` for the full command reference.
