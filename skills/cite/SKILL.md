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
`uv run cite ...` from this repo's root in dev. The examples below use bare
`cite`; add the `uv run` prefix if it is not on your PATH. **Every command
prints one JSON object** with a `status` field — read it and follow
`suggested_next` / `hint`.

## The contract lives in the tool, not here

**Run `cite guide --json` before your first write command in a session.** It is
generated from the installed version and is the authority on the command list,
the allowed citation types, the required fields for each, flag behaviour,
dedup tiers, extraction engines, and library resolution. This file does not
repeat those — where it seems to disagree with `guide`, `guide` is right.

## How to think about the work

- **Never invent bibliographic facts.** On `status: missing_fields`, ask the
  **user** for exactly the fields listed in `missing`, confirm, then re-run.
  Re-read the peek/prepare output first — the answer is often already there.
- **Never create a library implicitly.** `status: no_library` means confirm the
  intended path with the user before `cite init`. A typo must not spin up a
  stray library.
- **Duplicates are yours to triage.** `definitive` / `strong` near-duplicate
  matches you may resolve yourself; show `possible` matches to the user before
  re-running with `--force`.
- **Fix, don't re-add.** Use `cite update` to correct stored metadata rather
  than remove + add.

## Workflow skeleton (adding a file)

1. **Peek first** — `cite peek <file>` is milliseconds and enough for most
   publisher PDFs. Branch: if it comes back thin (no DOI, no title, a scan or
   untagged report), use `cite prepare <file>` instead and read the markdown
   head. A later `add` adopts the cached extraction, so this costs nothing extra.
2. **Search** — by `--doi` if you have one, else `--title` (+ `--author`,
   `--year`).
3. **Add** — pipe the single best candidate: `echo '<candidate-json>' | cite add <file> --csl -`.
   Branch: no database match → `cite add <file> --manual --type ... --field ...`.
4. **Bare URL, no local file** — `cite add-url <url>`. An HTML page becomes a
   `web-site` record; a PDF comes back as `status: downloaded` with a path,
   which re-enters this workflow at step 2.

## Cost and latency — choose the cheap path

- `peek` ≪ `prepare`. Don't extract a document whose metadata you already have.
- `cite extract --engine auto` (the default) reads a born-digital PDF through
  its own text layer in seconds. Only a scan falls through to the vision model,
  which takes minutes — **background that case** and poll `cite text <id>
  --path-only`. Never force `--engine vlm` on a document that has a text layer:
  it is slower *and* worse.
- `--enrich formula` costs minutes plus a ~600 MB first-run download. Use it
  only when the extracted markdown contains `<!-- formula-not-decoded -->` —
  that placeholder means the equations the paper is about are missing. Never add
  it to a document with no maths in it.

## Supplementary material belongs to its paper

Supporting information, supplementary tables and extended methods attach to an
**existing** reference: `cite add-supplement <id> <file> --label "<l>"`, read
back with `cite text <id> --supplement <n>`.

**Never `cite add` these as references of their own.** They have no citation
metadata, cannot produce a well-formed id, and would appear in `list` and
`export` as citable works.

## If a result looks impossible

Run `cite doctor --self` — it reports which extras are installed, whether
`cite-mcp` can start, and which library resolved and how. An empty result is
always an empty library, never a wrong path; a wrong path returns
`status: no_library`.

Full-markdown extraction needs the `extract` extra. On `status: error` with an
install hint, tell the user to install `cite[extract]` — never treat its absence
as a failure of the core workflow.
