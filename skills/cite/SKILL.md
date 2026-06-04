---
name: cite
description: Add a file to the citation/reference manager and produce a standard citation. Use when the user asks to add a paper, report, dataset, book, or web page to their reference library, citation manager, or "the library"; to look up a citation by DOI or title; or to rename/catalogue a document by its bibliographic details. Wraps the deterministic `cite` CLI.
---

# cite — citation/reference manager

`cite` is a deterministic CLI. You do the judgement (identify the file, pick the
right database match, ask the user for missing facts); the tool does the
mechanical work (search, validate, hash, rename, write the record). Keep tokens
low by letting the tool do the deterministic parts and reading its JSON output.

Run commands with `uv run cite ...` from the project root. **Every command
prints one JSON object** with a `status` field — read it and follow
`suggested_next` / `hint`.

## The contract
Run `uv run cite guide --json` once to load the full command list, the 7
allowed citation types, and the required fields for each. Do this if you are
unsure of a flag or which fields a type needs.

## Standard workflow (adding a file)
1. **Identify**: `uv run cite peek <file>` → returns an embedded-metadata
   title/author and any DOI found in the first pages. Use `suggested_next`.
2. **Search**: if a DOI was found, `uv run cite search --doi <doi>`. Otherwise
   `uv run cite search --title "<title>" [--author <surname>] [--year <yyyy>]`.
3. **Add the match**: pick the best candidate from `.candidates`, then pipe just
   that one object: `echo '<candidate-json>' | uv run cite add <file> --csl -`.
   The tool validates, content-hashes (dedup), renames the file, and writes the
   record. A `status: duplicate` means the same bytes are already filed.
4. **No database match** (internal reports, etc.): pick the right type, and add
   manually. If you are missing required fields the tool tells you which:
   `uv run cite add <file> --manual --type <cite_type> --field title=... --field author="Smith, Jane; Lee, Kim" --field issued=2023 ...`
   - `author` is `Family, Given; Family2, Given2` (a comma-less entry becomes an
     organisational/literal name). `issued`/`accessed` are `YYYY[-MM[-DD]]`.
   - On `status: missing_fields`, ask the **user** for exactly the `missing`
     fields, get their confirmation, then re-run with those `--field`s added.
     Do not invent bibliographic facts — confirm them.

## The 7 citation types
`journal-article`, `book`, `book-section`, `web-site`, `trial-report`,
`data-set`, `other-report`. (See `cite guide` for required fields per type.)

## Other commands
- `uv run cite list` / `uv run cite get <id>` — browse the library.
- `uv run cite remove <id> [--delete-file]` — remove a record.
- `uv run cite export --format csl|bibtex|pandoc` — emit standard formats.

Library location: `--library <path>`, else `$CITE_LIBRARY`, else `./library`.
