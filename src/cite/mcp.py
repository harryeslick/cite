"""Thin MCP wrapper over the ``cite`` CLI.

Adds no bibliographic logic: every tool shells out to ``cite <subcommand>`` and
returns its JSON stdout unchanged. The CLI is the single source of truth — if the
server and the CLI ever disagree, the CLI wins.

The tool surface mirrors the CLI's *deterministic operations*, not its verb list:
``add`` is split into ``add_by_doi`` / ``add_from_csl`` / ``add_manual`` (three
distinct operations with different required inputs), while ``search`` stays a
single tool with optional filters. There are deliberately no composite tools
(e.g. peek->search->add) so the agent keeps the two judgement points: picking the
search candidate, and confirming missing fields with the user.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from cite.models import CiteType

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as e:  # friendly error if the [mcp] extra isn't installed
    raise SystemExit(
        "cite-mcp needs the 'mcp' extra: uv tool install 'cite[mcp]'"
    ) from e

_INSTRUCTIONS = """\
cite is a deterministic citation/reference manager. You supply the judgement; the
tools do the reproducible bookkeeping. Every tool returns one JSON object with a
`status` field — read it and follow any `hint` / `suggested_next`.

To ADD a file, always work the workflow in order — do not jump to add_manual:
  1. Identify the document:
       • prepare(file) — PREFERRED when the `extract` extra is installed. Extracts
         the full markdown once (cached by content hash) and returns its `head`
         plus any `doi`/`title_guess` read from the text — the best context for
         reports with no DOI / thin metadata. If it returns `extractor_unavailable`,
         fall back to peek.
       • peek(file)    — cheap, always available: recovers an embedded DOI / title
         / author. Use when prepare is unavailable, or the file clearly carries a
         printed DOI (peek + search is then faster than a full extraction).
  2. search(...)  — by the DOI found, else by title (+ author / year).
  3. File the chosen candidate:
       • a DOI search returns one full record   → add_from_csl(file, csl=<it>).
       • a title search returns compact summaries → pick one and file it by its
         `source_id`: add_by_doi(file, doi=<source_id>) (re-fetches the complete
         record). If a summary has no DOI, use add_manual.
  4. Only if search returns `empty`/`weak_match`: add_manual(file, type, fields).
     This is the last resort. On `missing_fields`, re-check the prepare `head` /
     peek output before asking the USER, and never fabricate bibliographic facts.

  A file prepared in step 1 has its extraction adopted automatically by any add in
  step 3/4 (same bytes) — the add result reports `extracted: true`, and the slow
  VLM pass is never repeated.

Call guide() once if you are unsure of a flag, a cite_type, or its required fields.

These tools are fast and deterministic: call them directly and read each JSON
envelope yourself. Do NOT shell out to the `cite` CLI (no subprocess, no wrapper
scripts) and do NOT delegate cite calls to a subagent — both discard the
structured envelope (and its provenance) and invite fabricated metadata.
"""

mcp = FastMCP("cite", instructions=_INSTRUCTIONS)

# Resolved once at import. uv tool install puts `cite` and `cite-mcp` in the same
# bin dir, so if this server is on PATH the CLI is too.
_CITE = shutil.which("cite")


def _error(message: str) -> str:
    return json.dumps({"status": "error", "message": message})


def _run(args: list[str], stdin: str | None = None) -> str:
    """Run `cite <args>` and return its stdout (already JSON) verbatim."""
    if _CITE is None:
        return _error("cite binary not found on PATH")
    # Force Typer's *plain* traceback for our subprocess only: the CLI keeps its
    # human-friendly Rich tracebacks, but here an uncaught exception must collapse
    # to a single final line so _last_line can lift the whole message (Rich wraps
    # it across terminal-width lines, which would truncate it).
    env = {**os.environ, "TYPER_STANDARD_TRACEBACK": "1"}
    proc = subprocess.run(
        [_CITE, *args], input=stdin, capture_output=True, text=True, env=env
    )
    # cite exits 0 for normal branches (missing_fields / duplicate / not_found);
    # a non-zero code means a bad argument or an uncaught crash. Surface only the
    # final, meaningful stderr line (e.g. "ValueError: ...") rather than the whole
    # traceback — the rest is frames the model doesn't need and would pay for.
    if proc.returncode != 0:
        return _error(_last_line(proc.stderr) or f"cite exited {proc.returncode}")
    return proc.stdout


def _last_line(stderr: str, limit: int = 500) -> str:
    """The last non-empty line of stderr, truncated — the actual error message."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    return lines[-1][:limit]


def _lib(library: str | None) -> list[str]:
    return ["--library", library] if library else []


def _force(force: bool) -> list[str]:
    return ["--force"] if force else []


# --------------------------------------------------------------------------- #
# Read / lookup tools
# --------------------------------------------------------------------------- #


@mcp.tool()
def guide() -> str:
    """Return the full cite usage contract (commands, 7 cite_types, required fields)."""
    return _run(["guide", "--json"])


@mcp.tool()
def peek(file: str, max_pages: int = 5) -> str:
    """Extract an embedded DOI/title/author from a local file without adding it.
    Call this tool directly — never via the `cite` CLI or a script."""
    return _run(["peek", file, "--max-pages", str(max_pages)])


@mcp.tool()
def prepare(
    file: str,
    head_chars: int = 2000,
    vlm_model: str = "granite_docling",
    library: str | None = None,
) -> str:
    """Extract a file's full markdown BEFORE adding it, for better citation context.

    Prefer this over `peek` as the first step WHEN the `extract` extra is installed
    — especially for reports with no DOI and thin embedded metadata, where the
    document text is the best source of title/author/publisher/date. It runs the
    local Docling VLM once, caches the markdown by content hash, and returns
    `status: staged` with the markdown `head`, any `doi`/`title_guess` read from
    the text, and a `markdown_path` you can read in full. Then `search` (better
    informed) or `add_manual`; the later add adopts the cached extraction, so the
    slow VLM pass never repeats (the add result reports `extracted: true`).

    Degrades cleanly: returns `status: extractor_unavailable` (with a `cite peek`
    fallback in `suggested_next`) if the extra isn't installed, and `status:
    duplicate` if the bytes are already filed.

    Long-running: a vision model runs over the whole document and can take minutes;
    this MCP call blocks until it finishes. If the file is clearly in a database
    (a DOI is printed on it), `peek` + `search` may be faster.
    """
    return _run([
        "prepare", file, "--head-chars", str(head_chars),
        "--vlm-model", vlm_model, *_lib(library),
    ])


@mcp.tool()
def search(
    doi: str | None = None,
    title: str | None = None,
    author: str | None = None,
    year: int | None = None,
) -> str:
    """Search Crossref/DataCite (by DOI) or OpenAlex (by title/author/year).

    Provide `doi`, or `title` (optionally narrowed by `author`/`year`).
    A DOI search returns one full CSL candidate → file with `add_from_csl`.
    A title search returns compact `candidates` summaries (title, authors, year,
    DOI) already filtered for relevance → pick one and file by its `source_id`
    with `add_by_doi`. status 'weak_match' or 'empty' means go to `add_manual`.
    """
    args = ["search"]
    if doi:
        args += ["--doi", doi]
    if title:
        args += ["--title", title]
    if author:
        args += ["--author", author]
    if year is not None:
        args += ["--year", str(year)]
    return _run(args)


# --------------------------------------------------------------------------- #
# Add tools — one per deterministic operation (see module docstring)
# --------------------------------------------------------------------------- #


@mcp.tool()
def add_by_doi(
    file: str, doi: str, force: bool = False, library: str | None = None
) -> str:
    """Fetch a citation by DOI (Crossref/DataCite), then validate/hash/rename/store.

    Get the `doi` from `peek(file)` (it reads any embedded DOI) rather than
    guessing it. Returns status 'not_found' if no DB match — fall back to add_manual.
    Returns status 'near_duplicate' if a same-work record is already filed; see
    `add_from_csl` for how to handle it.
    """
    return _run(["add", file, "--doi", doi, *_force(force), *_lib(library)])


@mcp.tool()
def add_from_csl(
    file: str, csl: str, force: bool = False, library: str | None = None
) -> str:
    """Add a file using a CSL-JSON record you chose — typically one candidate
    object from a `search` result. The record is piped to `cite add --csl -`.

    Returns status 'duplicate' if identical file bytes are already filed.
    Returns status 'near_duplicate' (with tiered `candidates`) if a *same work* is
    already filed under different bytes — e.g. a preprint vs published version. A
    `definitive`/`strong` candidate you may resolve yourself; a `possible` candidate
    is ambiguous, so confirm with the USER. To commit anyway, re-call with force=True
    (this overrides only the near-duplicate gate, never the identical-bytes gate).
    """
    return _run(["add", file, "--csl", "-", *_force(force), *_lib(library)], stdin=csl)


@mcp.tool()
def add_manual(
    file: str,
    type: CiteType,
    fields: dict[str, str],
    force: bool = False,
    library: str | None = None,
) -> str:
    """Add a file with no DB match by building the record from fields.

    LAST RESORT — only after `peek` + `search` turn up nothing. peek often
    recovers the title/author/date embedded in the document, so reach for it
    (and `search`) before entering fields by hand.

    `type` is one of the 7 cite_types; `fields` expands to repeated --field k=v
    (author = 'Family, Given; ...'; issued/accessed = 'YYYY[-MM[-DD]]').
    On status 'missing_fields', ask the USER for the listed fields and re-call —
    never fabricate bibliographic facts. On status 'near_duplicate', see
    `add_from_csl`; re-call with force=True to commit anyway.
    """
    args = ["add", file, "--manual", "--type", type, *_force(force), *_lib(library)]
    for key, value in fields.items():
        args += ["--field", f"{key}={value}"]
    return _run(args)


@mcp.tool()
def add_url(url: str, force: bool = False, library: str | None = None) -> str:
    """Add a citation from a URL — for a website you have only the link to.

    Fetches the page and branches on its type. An HTML page is snapshotted (the
    archived document) and its metadata (Open Graph / title / JSON-LD) parsed into
    a `web-site` record with today's `accessed` date. A PDF is downloaded and
    `peek`ed instead, returning status 'downloaded' with the saved `path` — then
    continue the normal document flow (`search` -> `add_by_doi`/`add_from_csl`),
    exactly as for a local PDF.

    On status 'missing_fields' (e.g. the page had no readable title), ask the USER
    for the field and re-add — never fabricate it. On status 'near_duplicate' the
    same URL is already filed; re-call with force=True to add anyway. Use this only
    when you have a bare URL: for a paper with a DOI, peek/search + add_by_doi gives
    richer metadata.
    """
    return _run(["add-url", url, *_force(force), *_lib(library)])


@mcp.tool()
def validate(type: str, csl: str) -> str:
    """Report which required fields a CSL-JSON record is missing for a cite_type."""
    return _run(["validate", "--type", type, "--csl", "-"], stdin=csl)


# --------------------------------------------------------------------------- #
# Library management tools
# --------------------------------------------------------------------------- #


@mcp.tool(name="list")
def list_refs(library: str | None = None, full: bool = False) -> str:
    """List references in the library (summaries, or full records with `full`)."""
    return _run(["list", *_lib(library), *(["--full"] if full else [])])


@mcp.tool()
def get(id: str, library: str | None = None) -> str:
    """Return one full CSL-JSON record by id."""
    return _run(["get", id, *_lib(library)])


@mcp.tool()
def doctor(library: str | None = None) -> str:
    """Library health check: invalid JSON, missing required fields, missing/orphan files."""
    return _run(["doctor", *_lib(library)])


@mcp.tool()
def update(
    id: str,
    fields: dict[str, str] | None = None,
    remove_fields: list[str] | None = None,
    type: CiteType | None = None,
    library: str | None = None,
) -> str:
    """Amend a stored reference's metadata in place — fix a wrong or missing field.

    Use this instead of remove + re-add when only the metadata is wrong (an
    off-by-one year, a typo'd title, a missing publisher): the attached document,
    its content hash, and provenance are preserved. `fields` sets/replaces (same
    mini-language as add_manual: author = 'Family, Given; ...', issued/accessed =
    'YYYY[-MM[-DD]]'); `remove_fields` deletes CSL keys; `type` changes the
    cite_type. Editing an id-bearing field (title, author/editor, year) re-stems
    the reference — the returned `id` then differs and `renamed` is true.

    On status 'missing_fields' the edit would leave the record incomplete for its
    type; nothing is committed — supply the field and re-call. Returns status
    'not_found' for an unknown id, 'error' if no change was given.
    """
    args = ["update", id, *_lib(library)]
    if type:
        args += ["--type", type]
    for key, value in (fields or {}).items():
        args += ["--field", f"{key}={value}"]
    for key in remove_fields or []:
        args += ["--remove-field", key]
    return _run(args)


@mcp.tool()
def remove(id: str, delete_file: bool = False, library: str | None = None) -> str:
    """Remove a record (and optionally its stored file)."""
    args = ["remove", id, *_lib(library)]
    if delete_file:
        args.append("--delete-file")
    return _run(args)


@mcp.tool()
def export(format: str = "csl", library: str | None = None) -> str:
    """Export the library as csl | bibtex | pandoc."""
    return _run(["export", "--format", format, *_lib(library)])


@mcp.tool()
def extract(
    id: str, vlm_model: str = "granite_docling", library: str | None = None
) -> str:
    """Extract full markdown for a stored reference via a local Docling VLM.

    Optional feature: needs the `extract` extra on the cite install. Stores the
    markdown + referenced images in the reference's bundle and records the
    extractor/version in `_provenance.extraction`. Returns status 'error' with an
    install hint if docling isn't available.

    Long-running: a vision model runs over the whole document and can take
    minutes. This MCP call blocks until it finishes and cannot be backgrounded
    over MCP, so finish all other cite work first and call this last. For
    fire-and-forget, run the `cite extract <id>` CLI as a background process
    instead and poll `cite text <id> --path-only` for completion.
    """
    return _run(["extract", id, "--vlm-model", vlm_model, *_lib(library)])


@mcp.tool()
def text(id: str, path_only: bool = False, library: str | None = None) -> str:
    """Return a reference's extracted markdown (or its path with path_only=True).

    Returns a JSON 'not_found' envelope if the reference hasn't been extracted yet.
    """
    args = ["text", id, *_lib(library)]
    if path_only:
        args.append("--path-only")
    return _run(args)


def main() -> None:
    """Entry point for the `cite-mcp` console script (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
