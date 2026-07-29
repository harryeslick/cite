---
name: cite
description: Add a file to the citation/reference manager and produce a standard citation. Use when the user asks to add a paper, report, dataset, book, or web page to their reference library, citation manager, or "the library"; to look up a citation by DOI or title; or to rename/catalogue a document by its bibliographic details. Wraps the deterministic `cite` CLI.
---

# cite — citation/reference manager

`cite` is a deterministic CLI. You do the judgement (identify the file, pick the
right database match, ask the user for missing facts); the tool does the
mechanical work (search, validate, hash, rename, write the record). Keep tokens
low by letting the tool do the deterministic parts and reading its JSON output.

Run commands as `cite ...` if installed globally (`uv tool install`), or
`uv run cite ...` from this repo's root in dev. The examples below use `uv run`;
drop that prefix when `cite` is on your PATH. **Every command prints one JSON
object** with a `status` field — read it and follow `suggested_next` / `hint`.

## The contract
Run `uv run cite guide --json` once to load the full command list, the 7
allowed citation types, and the required fields for each. Do this if you are
unsure of a flag or which fields a type needs.

## Standard workflow (adding a file)
1. **Confirm the library exists**: writing commands (`add`, `add-url`,
   `prepare`) require a `cite.toml` marker. If the tool reports no initialized
   library, confirm the intended path with the user, then run
   `uv run cite init --library <path> --yes`. Never let a typo create a stray
   library implicitly.
2. **Identify**: start with `uv run cite peek <file>` — it reads embedded PDF
   metadata and any printed DOI in milliseconds, and for most publisher PDFs
   that is enough to go straight to `search`. When peek comes back thin (no DOI,
   no title, a scanned or untagged report), use `uv run cite prepare <file>`:
   it extracts markdown once into a staging cache, returns the markdown `head`,
   and a later `add` adopts the cached extraction.
3. **Search**: if a DOI was found, `uv run cite search --doi <doi>`. Otherwise
   `uv run cite search --title "<title>" [--author <surname>] [--year <yyyy>]`.
4. **Add the match**: pick the best candidate from `.candidates`, then pipe just
   that one object: `echo '<candidate-json>' | uv run cite add <file> --csl -`.
   The tool validates, content-hashes (dedup), renames the file, and writes the
   record. DOI lookup can also be filed with `uv run cite add <file> --doi <doi>`.
   A `status: duplicate` means the same bytes are already filed.
5. **No database match** (internal reports, etc.): pick the right type, and add
   manually. Re-check the prepared markdown head / peek output before asking the
   user. If required fields are missing the tool tells you which:
   `uv run cite add <file> --manual --type <cite_type> --field title=... --field author="Smith, Jane; Lee, Kim" --field issued=2023 ...`
   - `author` is `Family, Given; Family2, Given2` (a comma-less entry becomes an
     organisational/literal name). `issued`/`accessed` are `YYYY[-MM[-DD]]`.
   - On `status: missing_fields`, ask the **user** for exactly the `missing`
     fields, get their confirmation, then re-run with those `--field`s added.
     Do not invent bibliographic facts — confirm them.
6. **Bare URL**: use `uv run cite add-url <url>` only when the user has a web
   address rather than a local file. HTML pages are snapshotted as `web-site`
   records. PDFs are downloaded and returned with `status: downloaded`; continue
   with the normal document workflow using the returned path.

## The 7 citation types
`journal-article`, `book`, `book-section`, `web-site`, `trial-report`,
`data-set`, `other-report`. (See `cite guide` for required fields per type.)

## Duplicate / correction rules
- `status: duplicate` means identical file bytes are already filed — stop.
- `status: near_duplicate` means the same work may already be filed. You may
  resolve `definitive` / `strong` matches yourself; show `possible` matches to
  the user before re-running with `--force`.
- Use `uv run cite update <id> --field key=value ...` to fix stored metadata
  instead of remove + re-add. Changing id-bearing fields may re-stem the bundle.
- Record ids are emitted as `cite:<stem>`; commands accept either that namespaced
  id or the bare stem.

## Other commands
- `uv run cite list` / `uv run cite get <id>` — browse the library.
- `uv run cite doctor` — health-check records, provenance, source files, and
  orphan bundles. `--self` checks the *install* instead (extras present,
  `cite-mcp` startable, which library resolved and how) — run it whenever a
  result looks impossible.
- `uv run cite remove <id>` — remove a record and its whole bundle.
- `uv run cite export --format csl|bibtex|pandoc` — emit standard formats.
- `uv run cite extract <id>` — full markdown for a stored reference.
  `--engine auto` (default) probes the text layer: a born-digital PDF is read
  through its own text in well under a minute even for a book, and only a scan
  falls through to `--engine vlm` (minutes — background that case). Never force
  `vlm` on a document that already has a text layer.

**No `--library` on `search` or `validate`** — they error if given one. `search`
queries the network; `validate` checks a CSL-JSON record on stdin against a
`--type` and takes no record id.

Library location: `--library <path>`, else `$CITE_LIBRARY`, else the nearest
`cite.toml` found by walking *up* from the current directory. Every command,
reads included, returns `status: no_library` rather than an empty result when
the resolved path has no `cite.toml`. Each reference is a self-contained bundle
dir `<id>/` (record + original file + any extracted markdown). A prepared file's
staged extraction is adopted by a later add of the same bytes, so extraction is
not repeated.

## Optional: full-text markdown
- `cite prepare <file>` extracts markdown before adding, for citation context.
- `cite extract <id>` converts a stored reference to full markdown locally
  (Docling VLM); output goes to `<id>/<id>.md` (+ `<id>_artifacts/`). Needs the
  `extract` extra — on `status: error` with an install hint, tell the user to
  install `cite[extract]`; never treat its absence as a failure of the core
  workflow.
- `cite text <id> [--path-only]` prints the extracted markdown (or its path).
