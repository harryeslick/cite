# cite

A small **deterministic** citation/reference manager designed to be driven by an
AI agent. The agent does the fuzzy work (identify a file, choose the right
database match, ask the user for missing facts); `cite` does the mechanical,
reproducible work (search databases, validate, content-hash, rename, write
records) and emits JSON so the agent spends minimal tokens.

## Design: the deterministic / agent split

| Step                                             | Owner                        |
| ------------------------------------------------ | ---------------------------- |
| Read a file to find its DOI / title / authors    | agent (aided by `cite peek`) |
| Search reference databases for the full citation | **tool** (`cite search`)     |
| Pick the correct candidate                       | agent                        |
| Validate, hash, slugify filename, write record   | **tool** (`cite add`)        |
| Fill missing fields for internal docs            | agent ↔ user                 |

Records are stored as **CSL-JSON** (the Citation Style Language standard that
Zotero exports and Pandoc consumes), with a custom `_provenance` block alongside
the standard fields. CSL processors ignore the underscore key, so records stay
both standard-compliant and self-tracking.

## Install / run

**Develop in this repo** (the `uv run` prefix resolves `cite` from this project's
`.venv`):

```bash
uv sync --extra mcp   # install deps incl. the optional MCP server
uv run cite guide     # print the full agent-facing contract
uv run pytest         # run the test suite
```

**Install globally** to use `cite` from any directory (no `uv run`, no project
root) — `uv tool install` puts `cite` and `cite-mcp` on your PATH:

```bash
uv tool install --force --from git+https://github.com/harryeslick/cite.git 'cite[all]'
```

`[all]` pulls both optional extras — `mcp` (the `cite-mcp` server) and `extract`
(local Docling markdown extraction, heavy: torch + models). Ask for less if you
want less: `'cite[mcp]'` for the CLI plus the MCP server, or plain `'cite'` for
the CLI alone. `--force` overwrites an existing install, so the same line is also
how you upgrade to the current `main`.

To install from a local checkout instead — useful while developing:

```bash
uv tool install --force "/path/to/cite[all]"   # add -e for an editable install
```

### Library location

The library resolves from `--library <path>`, else `$CITE_LIBRARY`, else the
nearest `cite.toml` found by walking **up** from the current directory (checking
each ancestor both as a library itself and as the parent of a `library/`). So
commands work from anywhere inside a project, not just its root. A path you name
explicitly is used literally and never searched from — you asked for that path.

Two common setups:

- **One central library** (a single personal collection reachable everywhere) —
  add `export CITE_LIBRARY="$HOME/citations"` to your shell profile.
- **Per-project library** — keep a `library/` in the project root and let the
  upward search find it from any subdirectory.

`cite.toml` is the marker that makes a directory a real library — `cite init`
is the *only* command that creates one (it asks for confirmation unless you pass
`--yes`). Every other command that touches a library, **reads included**, refuses
a directory that lacks `cite.toml`: you get `status: no_library` and a non-zero
exit, never an empty result that looks like a healthy empty library. A typo'd
`--library` path therefore can't silently spin up a stray library *or* silently
report nothing.

If a result ever looks impossible, `cite doctor --self` reports which optional
extras are installed, whether the `cite-mcp` entrypoint can start, and which
library root resolved and how.

### Identifying yourself to the reference databases (optional)

Two settings, both optional — searches work without them, just on a smaller
allowance. They live in the library's `cite.toml`, which `cite init` writes with
the section commented out:

```toml
[search]
contact_email = "you@example.org"   # sent to CrossRef as a User-Agent mailto:
openalex_api_key = "..."            # free key: openalex.org/settings/api
```

| Setting            | Effect                                                                                                                                                                       |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `openalex_api_key` | OpenAlex bills API use against a daily budget: **$0.10/day** unauthenticated, **$1/day** with a free key from [openalex.org/settings/api](https://openalex.org/settings/api). |
| `contact_email`    | Adds `mailto:` to the User-Agent sent to CrossRef/DataCite — their requested convention for identified traffic.                                                               |

Both also read from `$CITE_CONTACT_EMAIL` / `$OPENALEX_API_KEY`, which
**take precedence** over the file, so `OPENALEX_API_KEY=... cite search ...`
overrides for one call.

Keeping them in `cite.toml` means they follow the library: a per-project library
carries its own identity, and the MCP server picks them up with no `--env`
wiring, since it resolves the same library your CLI does. The trade-off is that
a key in a `library/` inside a repo is a key you can commit — use the
environment variable for that case.

`cite doctor --self` reports both under `search_identity`, including which
source each came from. The key's value is never echoed, only whether one is set.

When a database can't be reached — rate limit, outage, timeout — `search` returns
`status: unavailable` (exit 1) with an `unavailable_sources` list, *not*
`not_found`. That distinction exists so an agent waits and retries instead of
hand-typing metadata for a paper the database would have returned a minute later.

Either way the library is laid out as a directory of **per-entity bundles** —
each reference is a self-contained directory named by its id:

```text
library/
  cite.toml                 # config
  <id>/<id>.json            # the CSL-JSON record for this reference
  <id>/<id>.<ext>           # the renamed original document file
  <id>/<id>.md              # (optional) extracted full markdown — see `cite extract`
  <id>/<id>_artifacts/      # (optional) referenced images for the markdown
```

The id (and file stem) follow `<year>-<author1>-<author2|etal|none>-<title>_<hash6>`,
generated deterministically from the record. The `<hash6>` is a short prefix of
the file's full SHA-256 (the full hash is stored in `_provenance` and used for
deduplication — identical bytes can't be filed twice). Keeping everything for one
reference in a single directory means it moves, syncs, and deletes atomically, and
the markdown's relative image links resolve in place.

## The 7 citation types → CSL

| `cite_type`       | CSL `type`        | notes                    |
| ----------------- | ----------------- | ------------------------ |
| `journal-article` | `article-journal` |                          |
| `book`            | `book`            |                          |
| `book-section`    | `chapter`         |                          |
| `web-site`        | `webpage`         | needs `URL` + `accessed` |
| `trial-report`    | `report`          | `genre: "trial report"`  |
| `data-set`        | `dataset`         |                          |
| `other-report`    | `report`          |                          |

Run `cite guide --json` for the required fields of each type.

## Commands

Every command prints a single JSON object/array with a `status` field. Commands
exit `0` even for `missing_fields` / `duplicate` — those are normal workflow
branches; inspect `status` rather than the exit code.

| Command                                                 | Purpose                                                               |
| ------------------------------------------------------- | --------------------------------------------------------------------- |
| `cite init [--library <path>] [--yes]`                  | Create a new library (writes `cite.toml`). The *only* way to create one — prompts for confirmation unless `--yes`. |
| `cite peek <file>`                                      | Deterministically extract embedded metadata + a DOI from a file.      |
| `cite search --doi <doi>`                               | Look up a DOI via CrossRef, then DataCite.                            |
| `cite search --title "<t>" [--author <a>] [--year <y>]` | Fuzzy search via OpenAlex.                                            |
| `cite add <file> --doi <doi>`                           | Fetch by DOI, then validate/hash/rename/store.                        |
| `cite add <file> --csl -`                               | Add from a CSL-JSON record on stdin (e.g. a chosen search candidate). |
| `cite add <file> --manual --type <t> --field k=v ...`   | Add a record built from fields.                                       |
| `cite validate --type <t> --csl -`                      | Report which required fields a record is missing.                     |
| `cite list [--full]`                                    | List references.                                                      |
| `cite get <id>`                                         | Print one record.                                                     |
| `cite doctor`                                           | Health-check the whole library (bad JSON, missing fields/files, orphans). |
| `cite doctor --self`                                    | Health-check the *install*: extras present, `cite-mcp` startable, which library resolved. |
| `cite remove <id>`                                      | Remove a reference (deletes its whole bundle directory).              |
| `cite extract <id> [--engine auto\|text\|vlm] [--enrich formula,code]` | Extract full markdown via a local Docling pipeline (optional, see below). |
| `cite add-supplement <id> <file> [--label "<l>"]`       | Attach supplementary material to an existing reference (see below).   |
| `cite text <id> [--path-only] [--supplement <n>]`       | Print a reference's extracted markdown (or its path), or a supplement's. |
| `cite export --format csl\|bibtex\|pandoc`              | Emit the library in a standard format.                                |
| `cite guide [--json]`                                   | Print the full agent-facing contract.                                 |

### `--field` syntax (manual add)

- `author` / `editor`: `"Family, Given; Family2, Given2"` — a comma-less entry
  becomes an organisational (literal) name.
- `issued` / `accessed` / `year`: `YYYY`, `YYYY-MM`, or `YYYY-MM-DD`.
- everything else: a plain string (`title`, `URL`, `DOI`, `container-title`,
  `publisher`, ...).

## Example: add an internal report

```bash
# Not in any database — build it manually.
uv run cite add trial.pdf --manual --type trial-report \
  --field title="Phase II Trial of Widget X" \
  --field author="Smith, Jane; Lee, Kim; Brown, Sam" \
  --field publisher="Internal Lab" \
  --field issued=2023
# -> status: added, id: 2023-smith-etal-phase-ii-trial-of-widget-x_8defac
```

If required fields are missing the tool returns `status: missing_fields` with
the exact `missing` list and a ready-to-edit `hint` — ask the user for those
fields, confirm, and re-run.

## Roadmap / future development

Ideas not yet implemented, in rough priority order:

- **Full-text search / RAG over the library** — a future *sibling* tool (not
  `cite` itself) that indexes the extracted markdown by `cite:<id>` and resolves
  display via `cite get`. Keeping the index (and its embedding/vector deps) in a
  separate single-job tool is what lets `cite` stay the lean foundation. The
  *extraction* half is now implemented (see "Local markdown extraction" below).

- **Cross-file dedup of different copies** of the same work, and additional
  search backends (PubMed, Semantic Scholar) — the `search/` package is
  structured to accept new backends with minimal change.

- **Citation dependency graph** *(likely a new sibling tool, not `cite` itself)*.
  Extract the references *within* each document and build a dependency graph of
  research papers — who cites whom — maintained as its own datastore. References
  need not be resolved online; a deterministic fuzzy match (reusing the near-dup
  DOI / title-author-year scoring above) identifies duplicates and links each
  extracted reference to a paper already in the graph. Per SUITE.md §1 the *agent*
  parses each bibliography (fuzzy work) and the tool deterministically scores and
  records the edges. Cleanest as a single-job sibling (e.g. `xref`) depending on
  `cite` via its published interface (`cite get <id>`), keeping `cite` the
  foundation that owns only the canonical library.

- **Topic-based citation lists.** A command (e.g. `cite list --topic <name>`)
  that creates a curated subset of the library filtered by topic, exported to a
  file or JSON object. Enables organization of citations by research area or
  project — e.g. "machine learning", "climate science" — without reorganizing
  the core library. Could be backed by metadata tags in the `_provenance` block
  or a simple topic-to-id mapping file.

- **centralised library** at user level. copies selected files into project dir when required. prevents user level duplication between projects.

## Supplementary material

A paper's supporting information, supplementary data tables and extended methods
belong **to** that paper — they carry no citation metadata of their own, so they
cannot produce a well-formed id, and adding them as separate references would put
them in `list` and `export` as if they were citable works. Attach them instead:

```bash
uv run cite add-supplement cite:2024-smith-a-study_a1b2c3 si.pdf     --label "Supporting Information S1"
uv run cite add-supplement cite:2024-smith-a-study_a1b2c3 table_s1.xlsx --label "Table S1"

uv run cite text cite:2024-smith-a-study_a1b2c3 --supplement 1       # read it back
uv run cite get  cite:2024-smith-a-study_a1b2c3                      # see what's attached
```

They live inside the parent's bundle, sharing its stem:

```
2024-smith-a-study_a1b2c3/
  2024-smith-a-study_a1b2c3.json          # _provenance.supplements: [...]
  2024-smith-a-study_a1b2c3.pdf           # the paper
  2024-smith-a-study_a1b2c3.md
  2024-smith-a-study_a1b2c3_supp01.pdf    # Supporting Information S1
  2024-smith-a-study_a1b2c3_supp01.md
  2024-smith-a-study_a1b2c3_supp02.xlsx   # Table S1
  2024-smith-a-study_a1b2c3_supp02.md
```

Because a reference is its directory, supplements need no relational bookkeeping:
`remove`, `pull`, central sync and the `update` re-stem all carry them along.

**Text is extracted where the file type allows.** PDFs and office documents go
through the Docling pipeline (needs the `extract` extra); `.csv`/`.tsv`/`.xlsx`
become markdown tables, one section per worksheet, needing nothing heavier than
openpyxl; anything else — a `.zip` of raw data — is stored as-is with no markdown.
**A file that cannot be read is still attached**, with a `warning` on the response
rather than an error, because an unreadable supplement is still worth keeping
beside its paper. Re-extract one later (say, with `--enrich formula`) via
`cite extract <id> --supplement <n>`; re-attaching identical bytes is a no-op.

## Local markdown extraction (optional)

`cite extract <id>` converts a stored reference's source document to full markdown
using [Docling](https://github.com/docling-project/docling) — **entirely on your
machine**, nothing is sent to a remote service. Images are exported as referenced
local files so figures render:

```bash
uv sync --extra extract            # dev: install docling (heavy: pulls torch + model)
# global installs of cite[all] (see "Install / run") already include the extractor

uv run cite extract cite:2022-agency-annual-report_abc123
uv run cite text   cite:2022-agency-annual-report_abc123   # print the markdown
```

### Choosing an engine

Most documents worth citing are born-digital: they already carry a perfect text
layer, and re-reading it with a vision model is slow *and* lossy. So `--engine`
defaults to `auto`, which probes the text layer first and picks:

| engine | what it does | when |
| --- | --- | --- |
| `text` | Docling's standard pipeline: layout + table-structure models over the PDF's own text, OCR'ing only pages that have none | born-digital PDFs |
| `vlm`  | `granite_docling` reads rendered pages | scans, image-only documents |

For scale: a 181-page born-digital book extracts in **~45 seconds** on the `text`
engine, against tens of minutes for the VLM — and the text engine cannot misread
a word that was already in the file. Force one with `--engine text` / `--engine
vlm` when you disagree with the probe.

### Equations: `--enrich formula`

The text engine reads what the PDF's text layer says, and typeset maths is the
one thing that layer routinely gets wrong — parentheses arriving as `ð`/`Þ`,
unmapped glyphs as `/C0`, sub/superscripts flattened. Docling will not guess at
a region it cannot decode, so by default it writes a placeholder and moves on:

```markdown
<!-- formula-not-decoded -->
```

For a modelling paper, that means the equations are **absent** from the markdown,
not merely ugly — and nothing downstream can tell, because the surrounding prose
extracted perfectly. `--enrich formula` re-reads each of those regions with a
small vision model (CodeFormulaV2) and emits LaTeX instead; `--enrich code` does
the same for code blocks, and `--enrich formula,code` runs both.

It is off by default because it is expensive. A 20-page modelling paper with 15
equations went from **12 s to 10 minutes** — a ~600 MB model download the first
time, then per-region inference on CPU. Turn it on for maths-heavy sources where
the equations are the point, leave it off otherwise, and background the call the
way you would a `vlm` run.

What you get back is real LaTeX, including detail the text layer had destroyed —
in that paper `fðCÞ` came back as `f(\Psi)`, the vision model reading a psi where
the font encoding had produced a Latin C. Equation numbers are sometimes swept
into the LaTeX alongside the equation; the maths is right, the trailing `(1)` may
sit inside the `$$`. It applies to the `text` engine only — combine
it with `--engine text`, and note that asking for it on a document that probes as
a scan is an error rather than a silent no-op, so an extraction never claims an
enrichment that did not run. Whatever ran is recorded as
`_provenance.extraction.enrichments`.

The markdown and its images are written into the reference's bundle as
`<id>/<id>.md` + `<id>/<id>_artifacts/`. The extractor name and version, the
engine used, and the probe that chose it are recorded under
`_provenance.extraction`, so you can tell later which kind of extraction you have
and whether it is stale. The feature is **optional**: the `extract`/`text`
commands return a `status: error` with an install hint when the `extract` extra
is absent, and the rest of `cite` works without it. Re-running `extract`
overwrites prior output.

## For agents

For **shell-capable agents** (Claude Code, Codex, …) the entry point in Claude
Code is the **`cite` skill** (`skills/cite/SKILL.md`); other agents read
`AGENTS.md` and call `cite guide --json` to load the contract. When `cite` is
installed globally, invoke it as plain `cite …` — `uv run cite …` is the in-repo
dev form.

### MCP server (`cite-mcp`)

For clients that call typed tools instead of a shell (Claude Desktop, Cursor,
…), `cite[mcp]` ships a thin MCP server, `cite-mcp`. It adds no logic — each tool
calls `cite.ops` in-process and returns the same JSON envelopes as the CLI, so
the CLI and MCP server share one deterministic core. The tools mirror the core
operations: `guide`, `init`, `peek`, `prepare`, `search`, `add_by_doi` /
`add_from_csl` / `add_manual`, `add_url`, `validate`, `list`, `get`, `doctor`,
`update`, `remove`, `extract`, `text`, `export`.

Register it (set `CITE_LIBRARY` so the server knows which library to use):

```bash
# Claude Code (project .mcp.json, or globally)
claude mcp add cite --env CITE_LIBRARY="$HOME/citations" -- cite-mcp
```

```json
// Claude Desktop — claude_desktop_config.json
{ "mcpServers": { "cite": { "command": "cite-mcp",
    "env": { "CITE_LIBRARY": "/Users/you/citations" } } } }
```

Stateful tools also accept a `library` argument to override per call.

`CITE_LIBRARY` is the only variable worth passing here: a host-launched server
does not inherit your shell profile, but the search identity comes from the
resolved library's `cite.toml`, so it needs no `--env` of its own.
