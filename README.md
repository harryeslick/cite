# cite

A small **deterministic** citation/reference manager designed to be driven by an
AI agent. The agent does the fuzzy work (identify a file, choose the right
database match, ask the user for missing facts); `cite` does the mechanical,
reproducible work (search databases, validate, content-hash, rename, write
records) and emits JSON so the agent spends minimal tokens.

## Design: the deterministic / agent split

| Step | Owner |
|------|-------|
| Read a file to find its DOI / title / authors | agent (aided by `cite peek`) |
| Search reference databases for the full citation | **tool** (`cite search`) |
| Pick the correct candidate | agent |
| Validate, hash, slugify filename, write record | **tool** (`cite add`) |
| Fill missing fields for internal docs | agent ↔ user |

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
# from this local checkout (works now, no git remote needed):
uv tool install "/path/to/cite[mcp]"      # add -e for an editable install
# or, once pushed to a remote, shareable:
uv tool install "cite[mcp] @ git+https://github.com/harryeslick/cite.git"
```

Drop the `[mcp]` extra if you only want the CLI. `uv tool update cite` re-pulls
from the same source.

### Library location

The library resolves from `--library <path>`, else `$CITE_LIBRARY`, else
`./library`. Two common setups:

- **One central library** (a single personal collection reachable everywhere) —
  add `export CITE_LIBRARY="$HOME/citations"` to your shell profile.
- **Per-project library** — pass `--library ./library`, or set a project-local
  `CITE_LIBRARY`, to scope citations to one project.

Either way the library is laid out as:

```
library/
  cite.toml                 # config
  refs/<id>.json            # one CSL-JSON record per reference
  files/<id>.<ext>          # the renamed document files
```

Filenames follow `<year>-<author1>-<author2|etal|none>-<title>_<hash6>.<ext>`,
generated deterministically from the record. The `<hash6>` is a short prefix of
the file's full SHA-256 (the full hash is stored in `_provenance` and used for
deduplication — identical bytes can't be filed twice).

## The 7 citation types → CSL

| `cite_type` | CSL `type` | notes |
|-------------|-----------|-------|
| `journal-article` | `article-journal` | |
| `book` | `book` | |
| `book-section` | `chapter` | |
| `web-site` | `webpage` | needs `URL` + `accessed` |
| `trial-report` | `report` | `genre: "trial report"` |
| `data-set` | `dataset` | |
| `other-report` | `report` | |

Run `cite guide --json` for the required fields of each type.

## Commands

Every command prints a single JSON object/array with a `status` field. Commands
exit `0` even for `missing_fields` / `duplicate` — those are normal workflow
branches; inspect `status` rather than the exit code.

| Command | Purpose |
|---------|---------|
| `cite peek <file>` | Deterministically extract embedded metadata + a DOI from a file. |
| `cite search --doi <doi>` | Look up a DOI via CrossRef, then DataCite. |
| `cite search --title "<t>" [--author <a>] [--year <y>]` | Fuzzy search via OpenAlex. |
| `cite add <file> --doi <doi>` | Fetch by DOI, then validate/hash/rename/store. |
| `cite add <file> --csl -` | Add from a CSL-JSON record on stdin (e.g. a chosen search candidate). |
| `cite add <file> --manual --type <t> --field k=v ...` | Add a record built from fields. |
| `cite validate --type <t> --csl -` | Report which required fields a record is missing. |
| `cite list [--full]` | List references. |
| `cite get <id>` | Print one record. |
| `cite remove <id> [--delete-file]` | Remove a record (and optionally its file). |
| `cite export --format csl\|bibtex\|pandoc` | Emit the library in a standard format. |
| `cite guide [--json]` | Print the full agent-facing contract. |

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

- **Near-duplicate detection before add.** Today the dedup gate only catches
  *identical bytes* (same content hash). Before committing a new file, also
  search the existing library for *similar* records — matching DOI, or close
  title/author/year — and surface any candidates so the **user can confirm**
  whether the new file is genuinely a new entry or a copy/version of one already
  filed. (Distinct from, and layered on top of, the existing exact-hash gate.)

- **Periodic library validation / health check** (e.g. `cite doctor`). Walk the
  whole library to keep it current and trustworthy: confirm every `refs/*.json`
  is valid JSON and still passes its type's required-field check, verify each
  record's `files/<new_filename>` source file is present and locatable (and flag
  orphan files with no record), and report anything stale or broken. Intended to
  be run on a schedule.

- **Full markdown extraction via a local model.** A function (e.g. `cite
  extract`) that converts a source document to full markdown using a local tool
  such as [Docling](https://github.com/docling-project/docling) (or similar),
  stored alongside the reference. Enables full-text search / RAG over the library
  while keeping processing local and private. Should stay optional so the core
  tool has no heavy model dependency.

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

## For agents

For **shell-capable agents** (Claude Code, Codex, …) the entry point in Claude
Code is the **`cite` skill** (`skills/cite/SKILL.md`); other agents read
`AGENTS.md` and call `cite guide --json` to load the contract. When `cite` is
installed globally, invoke it as plain `cite …` — `uv run cite …` is the in-repo
dev form.

### MCP server (`cite-mcp`)

For clients that call typed tools instead of a shell (Claude Desktop, Cursor,
…), `cite[mcp]` ships a thin MCP server, `cite-mcp`. It adds no logic — each tool
shells out to `cite` and returns its JSON unchanged, so the CLI stays the single
source of truth. The tools mirror the CLI's deterministic operations: `guide`,
`peek`, `search`, `add_by_doi` / `add_from_csl` / `add_manual`, `validate`,
`list`, `get`, `remove`, `export`.

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
