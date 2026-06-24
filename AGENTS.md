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
1. Ensure the library exists. `add` / `add-url` / `prepare` require a `cite.toml`
   marker and never create libraries implicitly. If missing, confirm the intended
   path with the **user**, then run `uv run cite init --library <path> --yes`.
2. Prefer `uv run cite prepare <file>` when the `extract` extra is installed:
   it extracts markdown once, returns a `head` plus DOI/title guesses, and a later
   add adopts the staged extraction. If unavailable, or a DOI is obvious, use
   `uv run cite peek <file>`.
3. `uv run cite search --doi <doi>` (or `--title "<t>" --author <a> --year <y>`).
4. Pick the best `.candidates` entry, then:
   `echo '<candidate-json>' | uv run cite add <file> --csl -`.
5. No DB match? Choose the right `--type` and add manually, supplying fields:
   `uv run cite add <file> --manual --type <cite_type> --field title=... --field author="Smith, Jane; Lee, Kim" --field issued=2023`.
   Re-check the prepared markdown head / peek output before asking the user.
   On `status: missing_fields`, ask the **user** for the listed `missing` fields,
   confirm, then re-run. Never fabricate bibliographic facts.
6. Bare URL? `uv run cite add-url <url>` snapshots HTML as a `web-site` record;
   if it returns `downloaded` for a PDF, continue the normal file workflow using
   the returned path.

## Rules
- Citation types are limited to: `journal-article`, `book`, `book-section`,
  `web-site`, `trial-report`, `data-set`, `other-report`.
- `status: duplicate` means identical file bytes are already filed — stop.
- `status: near_duplicate` means the same work may already be filed. You may
  resolve `definitive` / `strong` matches yourself; show `possible` matches to
  the user before re-running with `--force`.
- `author` field format: `Family, Given; ...`; a comma-less entry is a literal
  (organisational) name. `issued`/`accessed`: `YYYY[-MM[-DD]]`.
- Library path: `--library`, else `$CITE_LIBRARY`, else `./library`.
- Each reference is a self-contained bundle dir `<id>/` (record + original file +
  any extracted markdown). `cite remove <id>` deletes the whole bundle.
- Record ids are emitted as `cite:<stem>`; commands accept namespaced ids or bare
  stems.
- Use `cite update <id> --field key=value ...` to fix stored metadata instead of
  remove + re-add. Use `cite doctor` to health-check the library.

## Optional: full-text markdown
- `cite prepare <file>` extracts markdown before adding, for citation context,
  and the later add adopts the staged markdown for the same bytes.
- `cite extract <id>` converts a stored reference to full markdown locally (Docling
  VLM); output goes to `<id>/<id>.md` (+ `<id>_artifacts/`). Needs the `extract`
  extra — on `status: error` with an install hint, tell the user to install
  `cite[extract]`; never treat its absence as a failure of the core workflow.
- `cite extract <file>` also works without a library: pass a file path instead of
  an id and output goes beside the input file as `<stem>.md` (+ `<stem>_artifacts/`).
- `cite text <id> [--path-only]` prints the extracted markdown (or its path).

See `README.md` for the full command reference.
