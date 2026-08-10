"""Thin MCP wrapper over :mod:`cite.ops`.

Adds no bibliographic logic: every tool calls the matching ``cite.ops`` function
in-process and returns its envelope as JSON. ``ops`` is the single source of truth,
shared verbatim with the ``cite`` CLI — if the server and the CLI ever disagree,
they have diverged from the same core (they should not).

The tool surface mirrors the *deterministic operations*, not the CLI's verb list:
``add`` is split into ``add_by_doi`` / ``add_from_csl`` / ``add_manual`` (three
distinct operations with different required inputs), while ``search`` stays a
single tool with optional filters. There are deliberately no composite tools
(e.g. peek->search->add) so the agent keeps the two judgement points: picking the
search candidate, and confirming missing fields with the user.
"""

from __future__ import annotations

import json

from cite import ops
from cite.doctor import run_doctor, run_self_check
from cite.guide import guide as build_guide
from cite.models import SPEC_VERSION, CiteType
from cite.search import search as run_search
from cite import peek as peek_mod
from cite.validate import validate as run_validate

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


def _ok(result: object, *, raw: bool = False) -> str:
    """Serialise an ops envelope to JSON, stamping it with the suite spec version.

    Mirrors ``cli._emit``: a response *envelope* (a dict) is stamped with
    ``SPEC_VERSION`` (SUITE.md §3); a *raw* payload — the CSL-JSON record from
    ``get``, or a bare list from ``list --full`` — passes through unstamped, since
    the spec belongs to the envelope, not the stored record.
    """
    if isinstance(result, dict) and not raw and "spec" not in result:
        result = {**result, "spec": SPEC_VERSION}
    return json.dumps(result, ensure_ascii=False)


def _error(message: str) -> str:
    return json.dumps({"status": "error", "message": message})


# --------------------------------------------------------------------------- #
# Read / lookup tools
# --------------------------------------------------------------------------- #


@mcp.tool()
def guide() -> str:
    """Return the full cite usage contract (commands, 7 cite_types, required fields)."""
    return build_guide(as_json=True)


@mcp.tool()
def init(library: str | None = None, yes: bool = False) -> str:
    """Create a new cite library at `library` — the only way to create one.

    `add`/`add-url`/`prepare` now require an existing library (marked by
    `cite.toml`) and refuse to create one implicitly. Pass yes=True once a
    human has approved the location — the underlying CLI prompts for
    confirmation otherwise, which would hang a non-interactive subprocess."""
    # No prompt here: the MCP caller asserts approval via `yes`. ops.init assumes
    # the create decision is made (idempotent if already initialized).
    lib = ops.resolve_library(_path(library))
    return _ok(ops.init(lib))


@mcp.tool()
def peek(file: str, max_pages: int = 5) -> str:
    """Extract an embedded DOI/title/author from a local file without adding it.
    Call this tool directly — never via the `cite` CLI or a script."""
    return _ok(peek_mod.peek(_path(file), max_pages=max_pages))


@mcp.tool()
def prepare(
    file: str,
    head_chars: int = 2000,
    engine: str = "auto",
    vlm_model: str = "granite_docling",
    enrich: str = "",
    library: str | None = None,
) -> str:
    """Extract a file's full markdown BEFORE adding it, for better citation context.

    Prefer this over `peek` as the first step WHEN the `extract` extra is installed
    — especially for reports with no DOI and thin embedded metadata, where the
    document text is the best source of title/author/publisher/date. It runs the
    local Docling pipeline once, caches the markdown by content hash, and returns
    `status: staged` with the markdown `head`, any `doi`/`title_guess` read from
    the text, and a `markdown_path` you can read in full. Then `search` (better
    informed) or `add_manual`; the later add adopts the cached extraction, so the
    extraction never repeats (the add result reports `extracted: true`).

    `engine` is `auto` (default), `text`, or `vlm` — see the `extract` tool. On
    `auto` a born-digital document is read through its own text layer in seconds;
    only a scan falls through to the vision model, which can take minutes and
    blocks this call. The response reports the `engine` that actually ran.
    `enrich` works as on the `extract` tool; the markdown a later `add` adopts is
    whatever this call produced, so pass `enrich: "formula"` here for a paper
    whose equations you will need.

    Degrades cleanly: returns `status: extractor_unavailable` (with a `cite peek`
    fallback in `suggested_next`) if the extra isn't installed, and `status:
    duplicate` if the bytes are already filed.
    """
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    try:
        return _ok(ops.prepare(
            lib,
            _path(file),
            head_chars=head_chars,
            engine=engine,
            vlm_model=vlm_model,
            enrich=enrich,
        ))
    except ValueError as e:  # unknown engine or enrichment
        return _error(str(e))
    except Exception as e:  # docling runtime failure — surface, don't crash
        return _error(f"extraction failed: {e}")


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
    if not doi and not title:
        return _error("provide doi or title")
    return _ok(run_search(doi=doi, title=title, author=author, year=year))


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
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    return _ok(ops.add_by_doi(lib, _path(file), doi, force=force))


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
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    try:
        record = ops.load_csl(csl)
    except ValueError as e:
        return _error(str(e))
    return _ok(ops.add_from_csl(lib, _path(file), record, force=force))


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
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    # Pass the fields dict straight through (no --field k=v argv reconstruction).
    field_args = [f"{k}={v}" for k, v in fields.items()]
    try:
        return _ok(ops.add_manual(lib, _path(file), type, field_args, force=force))
    except ValueError as e:
        return _error(str(e))


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
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    return _ok(ops.add_url(lib, url, force=force))


@mcp.tool()
def validate(type: str, csl: str) -> str:
    """Report which required fields a CSL-JSON record is missing for a cite_type."""
    try:
        record = ops.load_csl(csl)
    except ValueError as e:
        return _error(str(e))
    return _ok(run_validate(type, record))


# --------------------------------------------------------------------------- #
# Library management tools
# --------------------------------------------------------------------------- #


@mcp.tool(name="list")
def list_refs(library: str | None = None, full: bool = False, central: bool = False) -> str:
    """List references in the library (summaries, or full records with `full`).

    Pass central=True to list the central ~/.cite/ library instead of the project library.
    """
    if central:
        lib = ops.resolve_central()
    else:
        lib = ops.resolve_library(_path(library))
        err = ops.require_initialized(lib)
        if err is not None:
            return _ok(err)
    return _ok(ops.library_view(lib, full=full))


@mcp.tool()
def get(id: str, library: str | None = None) -> str:
    """Return one full CSL-JSON record by id."""
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    result = ops.get(lib, id)
    # A found record is a raw CSL-JSON payload (no spec stamp); not_found is an
    # envelope. Distinguish on the sentinel status key — mirrors cli.get.
    return _ok(result, raw=result.get("status") != "not_found")


@mcp.tool()
def doctor(library: str | None = None, self_check: bool = False) -> str:
    """Library health check: invalid JSON, missing required fields, missing/orphan files.

    Pass self_check=True to check the *install* instead of the library's contents:
    which optional extras are present, whether the `cite-mcp` entrypoint can
    start, and which library root resolved and how. Call that first whenever a
    result looks impossible — an empty library you know has records, or an
    extract that reports the extra is missing.
    """
    lib = ops.resolve_library(_path(library))
    if self_check:
        return _ok(run_self_check(lib))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    return _ok(run_doctor(lib))


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
    lib = ops.resolve_library(_path(library))
    field_args = [f"{k}={v}" for k, v in (fields or {}).items()]
    try:
        return _ok(ops.update(
            lib, id,
            fields=field_args, remove_fields=remove_fields or [], cite_type=type,
        ))
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
def pull(id: str, library: str | None = None) -> str:
    """Copy a reference from the central ~/.cite/ library into the project library.

    Browse the central library with list(central=True), pick an id, and pull it.
    The copy is full and independent — the project stays self-contained. Returns
    status 'not_found' if absent centrally, 'duplicate' if the same file is already
    in the project library.
    """
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    return _ok(ops.pull(lib, id))


@mcp.tool()
def remove(id: str, delete_file: bool = False, library: str | None = None, central: bool = False) -> str:
    """Remove a record (and optionally its stored file).

    Pass central=True to remove from the central ~/.cite/ library instead.
    Project removal does NOT affect the central library.
    """
    if central:
        lib = ops.resolve_central()
    else:
        lib = ops.resolve_library(_path(library))
    return _ok(ops.remove(lib, id))


@mcp.tool()
def export(format: str = "csl", library: str | None = None) -> str:
    """Export the library as csl | bibtex | pandoc."""
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    try:
        return ops.export(lib, format)
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
def extract(
    id: str,
    engine: str = "auto",
    vlm_model: str = "granite_docling",
    enrich: str = "",
    supplement: int | None = None,
    library: str | None = None,
) -> str:
    """Extract full markdown for a document via a local Docling pipeline.

    Optional feature: needs the `extract` extra on the cite install.

    `engine` selects how the document is read:
      * `auto` (default) — probe the text layer and choose. Use this.
      * `text` — read the PDF's own text with layout/table models. Seconds to a
        minute even for a book, and it cannot misread text that is already there.
      * `vlm` — granite-docling reads rendered pages. Necessary only for scans.

    The response reports the `engine` that ran and the `probe` that chose it, and
    both are stored in `_provenance.extraction`.

    `enrich` is `"formula"`, `"code"`, or `"formula,code"` — off by default. Use
    `enrich: "formula"` for a maths-heavy paper: without it docling drops every
    equation it will not guess at, leaving a `<!-- formula-not-decoded -->`
    placeholder where the equation should be, so the markdown reads as if the
    paper had no maths in it. It is slow enough to plan around — a 20-page paper
    with 15 equations took 10 minutes against 12 seconds without it, plus a
    ~600 MB model download the first time — and it blocks this call, so treat it
    like a `vlm` run: finish other cite work first, or run the CLI
    `cite extract <id> --engine text --enrich formula` as a background process
    and poll `cite text <id> --path-only`. Text engine only — pass
    `engine: "text"` with it, or a scanned document will error rather than
    silently skip the enrichment.

    **Library mode** (when `id` is a record id): stores the markdown + referenced
    images in the reference's bundle. Returns status 'error' with an install hint
    if docling isn't available.

    **Standalone mode** (when `id` is a path to an existing file): no library
    needed. Output markdown + artifacts are written beside the input file.

    `supplement: N` re-extracts the Nth attached supplementary file instead of
    the reference's own document — use it to redo a supplement with `enrich`.

    Only the `vlm` engine is slow. It can take minutes, blocks this call, and
    cannot be backgrounded over MCP — so when a scan needs it, finish all other
    cite work first, or run the `cite extract <id> --engine vlm` CLI as a
    background process and poll `cite text <id> --path-only` for completion.
    """
    from pathlib import Path as _Path

    candidate = _Path(id)
    if candidate.suffix and supplement is None:
        return _ok(
            ops.extract_file(
                candidate, engine=engine, vlm_model=vlm_model, enrich=enrich
            )
        )
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    return _ok(
        ops.extract(
            lib, id, engine=engine, vlm_model=vlm_model, enrich=enrich,
            supplement=supplement,
        )
    )


@mcp.tool()
def add_supplement(
    id: str,
    file: str,
    label: str | None = None,
    engine: str = "auto",
    vlm_model: str = "granite_docling",
    enrich: str = "",
    library: str | None = None,
) -> str:
    """Attach supplementary material to an existing reference and extract its text.

    Use this for a paper's supporting information, supplementary data tables, or
    extended methods — anything published *with* a paper rather than as its own
    citable work. Do NOT `add` these as separate references: they have no citation
    metadata of their own, and adding them pollutes `list` and `export`. They are
    stored inside the parent's bundle and travel with it through update/pull/remove.

    Text is extracted where possible — PDFs and office documents through Docling,
    `.csv`/`.xlsx` converted to markdown tables, everything else (e.g. a `.zip`)
    stored without markdown. A file that cannot be read is still attached and the
    response carries a `warning`; re-attaching the same bytes returns 'duplicate'.

    Read the result with `text(id, supplement=N)`; the ordinals are listed under
    `_provenance.supplements` in `get(id)`.
    """
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    return _ok(
        ops.add_supplement(
            lib, id, _path(file), label=label,
            engine=engine, vlm_model=vlm_model, enrich=enrich,
        )
    )


@mcp.tool()
def text(
    id: str,
    path_only: bool = False,
    supplement: int | None = None,
    library: str | None = None,
) -> str:
    """Return a reference's extracted markdown (or its path with path_only=True).

    Returns a JSON 'not_found' envelope if the reference hasn't been extracted yet.

    `supplement: N` returns an attached supplementary file's markdown instead;
    `get(id)` lists what is attached under `_provenance.supplements`.
    """
    lib = ops.resolve_library(_path(library))
    err = ops.require_initialized(lib)
    if err is not None:
        return _ok(err)
    result = ops.text(lib, id, path_only=path_only, supplement=supplement)
    if result.get("status") == "not_found":
        return _ok(result)
    # On a hit the markdown (or its path) is the payload, not an envelope — return
    # the bare string, matching the CLI's `cite text` stdout.
    return result["path"] if path_only else result["content"]


def _path(value: str | None):
    """Coerce an optional string path to a Path (ops accepts None → default root)."""
    from pathlib import Path

    return Path(value) if value is not None else None


def main() -> None:
    """Entry point for the `cite-mcp` console script (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
