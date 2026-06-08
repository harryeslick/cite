"""Agent-facing usage contract, generated from live models data."""

from __future__ import annotations

import json

from cite.models import CITE_TYPES, REQUIRED_FIELDS, SPEC_VERSION, TYPE_MAP

# --------------------------------------------------------------------------- #
# Static command descriptions
# --------------------------------------------------------------------------- #

_COMMANDS = [
    {"name": "peek", "summary": "Extract DOI/title/metadata from a local file without adding it (cheap, deterministic; no extra needed)."},
    {"name": "prepare", "summary": "Extract a file's full markdown BEFORE adding it (optional 'extract' extra), caching it by content hash and returning the markdown head + any DOI/title found. Best context for reports with no DOI/poor metadata. `cite add` later adopts the cache (no re-extract). Falls back to peek when the extra is absent."},
    {"name": "search", "summary": "Search for a reference by --doi or --title across Crossref/DataCite/OpenAlex."},
    {"name": "add", "summary": "Add a file to the library with CSL metadata (from --csl or --manual fields). Gated against identical-byte and same-work duplicates; --force commits past a near_duplicate."},
    {"name": "add-url", "summary": "Add a citation from a URL. An HTML page is snapshotted and its metadata (Open Graph/title/JSON-LD) parsed into a web-site record (accessed = today); a PDF is downloaded + peeked and handed back (status `downloaded`) for the normal search->add flow. URL dedup is exact (query/fragment ignored)."},
    {"name": "validate", "summary": "Check that a record has all required fields for its cite_type."},
    {"name": "list", "summary": "List all records in the library (brief summary view)."},
    {"name": "get", "summary": "Retrieve the full CSL-JSON record for a given record id."},
    {"name": "doctor", "summary": "Health-check the whole library: reports records with invalid JSON, missing provenance, missing required fields, a missing source file, or orphan bundles. Read-only; emits a summary plus one short line per problem."},
    {"name": "update", "summary": "Amend a stored record's metadata in place: --field key=value sets/replaces, --remove-field deletes, --type changes the cite_type. Re-validates and preserves the document/hash/provenance; editing an id-bearing field (title/author/year) re-stems the whole bundle (response reports `renamed`). Use instead of remove + re-add to fix a wrong field."},
    {"name": "remove", "summary": "Remove a record from the library (deletes the whole reference bundle)."},
    {"name": "extract", "summary": "Extract full markdown for a reference via a local Docling VLM (optional 'extract' extra). Slow/blocking — run in the background and poll `cite text` when your runtime allows."},
    {"name": "text", "summary": "Print a reference's extracted markdown, or its path with --path-only."},
    {"name": "export", "summary": "Export library records as CSL-JSON or another format."},
    {"name": "guide", "summary": "Print this agent usage contract (add --json for machine-readable form)."},
]

# --------------------------------------------------------------------------- #
# Workflow steps
# --------------------------------------------------------------------------- #

_WORKFLOW = [
    "Identify the document. If the `extract` extra is installed, prefer `cite prepare <file>`: "
    "it extracts the full markdown once (cached by content hash) and returns the markdown head "
    "plus any DOI/title read from the text — the best context for reports with no DOI or thin "
    "metadata. Read the head, then proceed to search/add (a later `cite add` adopts the cache, so "
    "the slow extraction never repeats). If `prepare` returns `extractor_unavailable` (or the extra "
    "isn't installed), fall back to `cite peek <file>`, which cheaply reads any embedded DOI/title.",
    "Run `cite search --doi <doi>` or `cite search --title <title>` to find candidates in Crossref/DataCite/OpenAlex.",
    "Pick the best match and run `cite add <file> --csl -` (pipe CSL-JSON on stdin) to add it.",
    "If no database match is found, gather required fields (run `cite validate` to see what's missing; "
    "for a prepared file read the markdown head for title/author/publisher/date — never fabricate) "
    "and run `cite add <file> --manual --type <type> --field key=value ...` to add manually.",
    "To cite a website you only have a URL for, run `cite add-url <url>`: it fetches the page, "
    "archives an HTML snapshot as the document, and fills a web-site record from the page's "
    "Open Graph / `<title>` / JSON-LD metadata (with `accessed` set to today). If the page had no "
    "readable title you get `missing_fields` — supply it and re-run, never fabricate. If the URL "
    "points at a PDF, `add-url` instead downloads it and returns `downloaded` with the saved `path`; "
    "continue with the normal document flow (`cite search` then `cite add <path>`).",
    "If `add` returns `near_duplicate`, the same work may already be filed. Inspect each "
    "`candidates[].tier`: a `definitive` (same DOI) or `strong` (same title+author+year) "
    "match you may resolve yourself (skip it, or keep both with `--force` for e.g. a "
    "preprint vs published version); a `possible` (fuzzy title) match is ambiguous — show "
    "the candidate(s) to the USER and confirm before re-running `cite add ... --force`.",
    "The tool returns the final CSL-JSON record and the new canonical filename assigned to the stored file.",
]

# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #

_NOTES = (
    "Records are stored as CSL-JSON dicts. Provenance metadata (original filename, "
    "new filename, date added, file hash, source) is stored under the `_provenance` key "
    "inside each record; CSL processors ignore underscore-prefixed keys. "
    "Deduplication has two gates. (1) Exact: an identical-bytes file (same SHA-256) is a "
    "no-op (`status: duplicate`); this gate is absolute and `--force` never overrides it. "
    "(2) Near-duplicate: before committing, the incoming record's metadata is compared to "
    "the library to catch the *same work* under different bytes, returning `status: "
    "near_duplicate` with tiered `candidates` (definitive = same DOI; strong = same "
    "title+author+year; possible = fuzzy title — ask the user). Pass `--force` to commit "
    "past the near-duplicate gate. `add-url` dedups differently: it compares the URL alone "
    "(query string and fragment stripped), so only an exact same-page match is rejected. "
    "Citation types are limited to the 7 listed in 'types'. "
    "Record ids are emitted in namespaced form `cite:<stem>` (the cross-tool foreign-key "
    "form); get/remove accept either the namespaced id or the bare stem. Every response "
    "envelope carries a `spec` field naming the protocol version (see 'spec'). "
    "Each reference is a self-contained bundle directory `<id>/` holding the record "
    "(`<id>.json`), the original file (`<id>.<ext>`), and any extracted markdown "
    "(`<id>.md` + `<id>_artifacts/`). Full-markdown extraction is an "
    "optional, fully-local feature requiring the `extract` extra (Docling); the rest of "
    "cite works without it. It runs either BEFORE add (`cite prepare <file>`, for citation "
    "context) or AFTER (`cite extract <id>`, for a stored reference) — same engine, and the "
    "markdown ends up at `<id>/<id>.md` either way. `prepare` caches its output under "
    "`<library>/.staging/<content-hash>/`; the next `cite add` of those exact bytes recomputes "
    "the hash, adopts the cached markdown into the bundle (its `added` envelope then reports "
    "`extracted: true` + `markdown_path`), and the VLM never runs twice. Extraction runs a "
    "vision model over the whole document and can take minutes, blocking until it finishes; if "
    "your runtime can run shell commands in the background, launch `cite prepare <file>` / "
    "`cite extract <id>` detached and keep doing other work, then poll "
    "`cite text <id> --path-only` (path appears only once extraction completes) instead of "
    "waiting on it inline."
)

# --------------------------------------------------------------------------- #
# Types table (built from live models data)
# --------------------------------------------------------------------------- #


def _build_types() -> list[dict]:
    types = []
    for cite_type in CITE_TYPES:
        csl_type, genre = TYPE_MAP[cite_type]
        reqs = REQUIRED_FIELDS.get(cite_type, [])
        required_fields = []
        for req in reqs:
            if isinstance(req, tuple):
                required_fields.append("|".join(req))
            else:
                required_fields.append(req)
        types.append(
            {
                "cite_type": cite_type,
                "csl_type": csl_type,
                "genre": genre,
                "required_fields": required_fields,
            }
        )
    return types


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def guide(as_json: bool = False) -> str:
    """Return the full agent usage contract.

    If as_json: return json.dumps of a dict with keys:
      'workflow' (ordered list of steps), 'commands' (list of {name, summary}),
      'types' (list of {cite_type, csl_type, genre, required_fields}), and
      'notes'. Build 'types' from TYPE_MAP + REQUIRED_FIELDS (render one-of
      groups as 'a|b'). Otherwise return a readable plain-text version of the same.
    """
    data = {
        "spec": SPEC_VERSION,
        "workflow": _WORKFLOW,
        "commands": _COMMANDS,
        "types": _build_types(),
        "notes": _NOTES,
    }

    if as_json:
        return json.dumps(data, indent=2, ensure_ascii=False)

    # Plain-text rendering
    lines: list[str] = []

    lines.append("=" * 72)
    lines.append("cite — Agent Usage Contract")
    lines.append("=" * 72)

    lines.append("\nWORKFLOW")
    lines.append("-" * 40)
    for i, step in enumerate(data["workflow"], 1):
        lines.append(f"  {i}. {step}")

    lines.append("\nCOMMANDS")
    lines.append("-" * 40)
    for cmd in data["commands"]:
        lines.append(f"  cite {cmd['name']:<12} {cmd['summary']}")

    lines.append("\nTYPES")
    lines.append("-" * 40)
    for t in data["types"]:
        genre_str = f"  [genre: {t['genre']}]" if t["genre"] else ""
        lines.append(f"  {t['cite_type']}")
        lines.append(f"    CSL type: {t['csl_type']}{genre_str}")
        lines.append(f"    Required: {', '.join(t['required_fields'])}")

    lines.append("\nNOTES")
    lines.append("-" * 40)
    lines.append(f"  {data['notes']}")

    lines.append("")
    return "\n".join(lines)
