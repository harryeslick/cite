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
import shutil
import subprocess

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as e:  # friendly error if the [mcp] extra isn't installed
    raise SystemExit(
        "cite-mcp needs the 'mcp' extra: uv tool install 'cite[mcp]'"
    ) from e

mcp = FastMCP("cite")

# Resolved once at import. uv tool install puts `cite` and `cite-mcp` in the same
# bin dir, so if this server is on PATH the CLI is too.
_CITE = shutil.which("cite")


def _error(message: str) -> str:
    return json.dumps({"status": "error", "message": message})


def _run(args: list[str], stdin: str | None = None) -> str:
    """Run `cite <args>` and return its stdout (already JSON) verbatim."""
    if _CITE is None:
        return _error("cite binary not found on PATH")
    proc = subprocess.run(
        [_CITE, *args], input=stdin, capture_output=True, text=True
    )
    # cite exits 0 for normal branches (missing_fields / duplicate / not_found);
    # a non-zero code means a bad argument — surface stderr as a structured error.
    if proc.returncode != 0:
        return _error(proc.stderr.strip() or f"cite exited {proc.returncode}")
    return proc.stdout


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
    """Extract an embedded DOI/title/author from a local file without adding it."""
    return _run(["peek", file, "--max-pages", str(max_pages)])


@mcp.tool()
def search(
    doi: str | None = None,
    title: str | None = None,
    author: str | None = None,
    year: int | None = None,
) -> str:
    """Search Crossref/DataCite (by DOI) or OpenAlex (by title/author/year).

    Provide `doi`, or `title` (optionally narrowed by `author`/`year`). Returns a
    `candidates` list — pick one and pass it to `add_from_csl`.
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

    Returns status 'not_found' if no DB match — fall back to add_manual.
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
    type: str,
    fields: dict[str, str],
    force: bool = False,
    library: str | None = None,
) -> str:
    """Add a file with no DB match by building the record from fields.

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
