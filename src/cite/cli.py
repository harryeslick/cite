"""The `cite` command-line interface — the agent-facing orchestrator.

This module is a thin CLI shell over :mod:`cite.ops`: each command parses argv,
reads stdin/files, prompts where a human decision is required, calls the matching
``ops`` function, and emits the returned envelope as JSON. The deterministic
orchestration (add/prepare/update/extract/url, dedup gates, bundle rename) lives
in ``ops`` so the ``cite-mcp`` server can share it in-process — see cite/ops.py.

Design notes for agents reading this:
  * Every command prints a single JSON object/array to stdout.
  * Commands exit 0 even for ``missing_fields`` or ``duplicate`` — those are
    normal branches of the workflow, not errors. Inspect the ``status`` key.
  * The library root resolves from --library, else $CITE_LIBRARY, else ./library.
"""

from __future__ import annotations

import json
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import typer

from cite import __version__
from cite import ops
from cite.guide import guide as build_guide
from cite.models import SPEC_VERSION
from cite.doctor import installed_extras, run_doctor, run_self_check
from cite.search import search as run_search
from cite.store import Library
from cite.validate import validate as run_validate
from cite import peek as peek_mod

app = typer.Typer(
    add_completion=False,
    help="A deterministic citation/reference manager for agent use.",
    no_args_is_help=True,
    # Keep Rich's pretty tracebacks ON: a human debugging `cite` at a terminal
    # wants the boxed frames + locals. The MCP server no longer shells out to this
    # binary — it calls cite.ops in-process — so it needs no traceback workaround.
)


# --------------------------------------------------------------------------- #
# Output + library helpers
# --------------------------------------------------------------------------- #


def _emit(obj: Any, *, raw: bool = False) -> None:
    """Print a JSON result to stdout (the one channel the agent parses).

    Every response *envelope* (a dict) is stamped with the suite spec version
    (SUITE.md §3) so agents and sibling tools can detect the protocol. Raw record
    payloads — e.g. the CSL-JSON record returned by `get` — pass through with
    raw=True: the spec belongs to the envelope, not to the stored record.
    """
    if isinstance(obj, dict) and not raw and "spec" not in obj:
        obj = {**obj, "spec": SPEC_VERSION}
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False))


def _resolve_library(library: Path | None) -> Library:
    return ops.resolve_library(library)


def _require_initialized(lib: Library) -> None:
    """Emit a clear error and exit if ``lib`` isn't a real library yet."""
    err = ops.require_initialized(lib)
    if err is not None:
        _emit(err)
        raise typer.Exit(code=1)


def _parse_enrich(enrich: str) -> tuple[str, ...]:
    """Validate ``--enrich`` up front so a typo fails before docling loads.

    The extractor validates too (it is the layer that has to be right), but
    doing it here turns a mid-run ValueError into typer's own usage error,
    the same way ``--engine`` is checked.
    """
    from cite.extract import normalize_enrich

    try:
        return normalize_enrich(enrich)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@app.command()
def version() -> None:
    """Report the cite version — a quick way to verify the install.

    Reads the *installed* distribution metadata (``importlib.metadata``) when
    available; that is what proves the package is actually installed on PATH and
    not merely importable from a source checkout. Falls back to the in-package
    ``__version__`` (and ``installed: false``) when no distribution is found.

    Also reports which optional extras are present under ``extras`` (a name→bool
    map) so a caller can tell at a glance whether ``mcp`` and/or ``extract`` are
    available without trying a command and getting an install hint.
    """
    try:
        ver = importlib_metadata.version("cite")
        installed = True
    except importlib_metadata.PackageNotFoundError:
        ver = __version__
        installed = False
    _emit(
        {
            "status": "ok",
            "tool": "cite",
            "version": ver,
            "installed": installed,
            "extras": installed_extras(),
        }
    )


@app.command()
def peek(
    file: Path = typer.Argument(..., exists=True, readable=True),
    max_pages: int = typer.Option(5, help="Pages to scan for a DOI."),
) -> None:
    """Deterministically extract a DOI / title / author guess from a file."""
    _emit(peek_mod.peek(file, max_pages=max_pages))


@app.command()
def prepare(
    file: Path = typer.Argument(..., exists=True, readable=True),
    head_chars: int = typer.Option(
        2000, help="How many leading characters of the extracted markdown to return."
    ),
    engine: str = typer.Option(
        "auto", help="auto | text | vlm — how to read the document."
    ),
    vlm_model: str = typer.Option(
        "granite_docling", help="Docling VLM model preset (used by the vlm engine)."
    ),
    enrich: str = typer.Option(
        "",
        help="formula,code — decode equations/code the text layer mangled (text "
             "engine only; minutes, and a ~600 MB model download on first use).",
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Extract a file's full markdown *before* adding it, for better citation context.

    The recommended first step when the ``extract`` extra is installed — especially
    for reports with no DOI and thin embedded metadata, where the document text
    itself is the best source of title / author / publisher / date. It runs the
    local Docling pipeline once, caches the markdown under the file's content hash
    in ``<library>/.staging/<hash>/``, and returns the markdown *head* plus any DOI /
    title it can read from it. Read the head, then ``cite search`` (now better
    informed) or build a manual record; the later ``cite add <file>`` recomputes
    the same hash and adopts this cached extraction, so extraction never runs twice.

    ``--engine`` works exactly as it does on ``cite extract``: ``auto`` reads a
    born-digital document through its own text layer and falls back to the vision
    model only for scans. The returned envelope reports which engine ran.
    ``--enrich formula`` likewise works as on ``cite extract``, and the staged
    markdown a later ``cite add`` adopts is the enriched one.

    Degrades cleanly: if the ``extract`` extra is not installed it returns
    ``status: extractor_unavailable`` pointing you at the deterministic fallback
    (``cite peek``) rather than erroring out. Only the ``vlm`` engine is slow
    enough to be worth backgrounding.
    """
    if engine not in ("auto", "text", "vlm"):
        raise typer.BadParameter("--engine must be one of: auto, text, vlm")
    enrich = _parse_enrich(enrich)
    lib = _resolve_library(library)
    _require_initialized(lib)
    try:
        result = ops.prepare(
            lib,
            file,
            head_chars=head_chars,
            engine=engine,
            vlm_model=vlm_model,
            enrich=enrich,
        )
    except Exception as e:  # docling runtime failure — surface, don't crash
        _emit({"status": "error", "message": f"extraction failed: {e}"})
        raise typer.Exit(code=1)
    _emit(result)


@app.command()
def search(
    doi: str | None = typer.Option(None, help="DOI to look up (CrossRef/DataCite)."),
    title: str | None = typer.Option(None, help="Title to search (OpenAlex)."),
    author: str | None = typer.Option(None, help="Author surname to bias search."),
    year: int | None = typer.Option(None, help="Publication year filter."),
) -> None:
    """Search reference databases for a citation by DOI or title/author."""
    if not doi and not title:
        raise typer.BadParameter("provide --doi or --title")
    _emit(run_search(doi=doi, title=title, author=author, year=year))


@app.command()
def add(
    file: Path = typer.Argument(..., exists=True, readable=True),
    doi: str | None = typer.Option(None, help="Fetch the citation by DOI, then add."),
    csl: str | None = typer.Option(
        None, help="Path to a CSL-JSON record, or '-' for stdin."
    ),
    manual: bool = typer.Option(False, help="Build the record from --field values."),
    type_: str | None = typer.Option(
        None, "--type", help="cite_type for --manual (e.g. other-report)."
    ),
    field: list[str] = typer.Option(
        [], "--field", help="key=value for --manual (repeatable)."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "--allow-near-duplicate",
        help="Commit even if a near-duplicate (same DOI / title+author+year) exists. "
        "Does NOT override the identical-bytes gate.",
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Add a file to the library: fetch/validate, hash, rename, write record."""
    lib = _resolve_library(library)
    _require_initialized(lib)

    if doi:
        _emit(ops.add_by_doi(lib, file, doi, force=force))
    elif csl is not None:
        # CLI owns the stdin/file read; ops.load_csl parses the text.
        text = sys.stdin.read() if csl == "-" else Path(csl).read_text()
        try:
            record = ops.load_csl(text)
        except ValueError as exc:
            raise typer.BadParameter(str(exc))
        _emit(ops.add_from_csl(lib, file, record, force=force))
    elif manual:
        if not type_:
            raise typer.BadParameter("--manual requires --type")
        try:
            result = ops.add_manual(lib, file, type_, field, force=force)
        except ValueError as exc:
            # An unknown cite_type (or other field-construction problem) is agent
            # error, not a bug — return the one-line reason as a structured
            # envelope rather than letting it escape as a traceback.
            _emit({"status": "error", "message": str(exc)})
            return
        _emit(result)
    else:
        raise typer.BadParameter("provide one of --doi, --csl, or --manual")


@app.command(name="add-url")
def add_url(
    url: str = typer.Argument(..., help="The web address to cite."),
    force: bool = typer.Option(
        False,
        "--force",
        "--allow-near-duplicate",
        help="Commit even if a page with the same URL is already filed. "
        "Does NOT override the identical-bytes gate.",
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Add a citation from a URL: fetch the page, snapshot it, and read its metadata.

    Fetches `url` and branches on the response Content-Type:
      • An HTML page is archived as the reference's document (`<id>.html`) and its
        Open Graph / `<title>` / JSON-LD metadata is parsed into a `web-site`
        record. `accessed` is set to today; on `missing_fields` (e.g. no title
        could be read), supply the field and re-run — never fabricate it.
      • A PDF (by Content-Type or a `%PDF` sniff) is downloaded to the library's
        `.downloads/` and `peek`ed, then handed back with status `downloaded` so
        you continue with the normal document flow (search -> `cite add <path>`).

    URL dedup is exact: a page whose normalized URL (query string and fragment
    stripped) already exists returns `near_duplicate`; pass --force to add anyway.
    """
    lib = _resolve_library(library)
    _require_initialized(lib)
    _emit(ops.add_url(lib, url, force=force))


@app.command()
def validate(
    type_: str = typer.Option(..., "--type", help="cite_type to validate against."),
    csl: str = typer.Option("-", help="CSL-JSON record path, or '-' for stdin."),
) -> None:
    """Check a record against its type's required fields; list what's missing."""
    text = sys.stdin.read() if csl == "-" else Path(csl).read_text()
    try:
        record = ops.load_csl(text)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))
    _emit(run_validate(type_, record))


@app.command(name="list")
def list_(
    library: Path | None = typer.Option(None, help="Library root."),
    full: bool = typer.Option(False, help="Emit full records instead of summaries."),
    central: bool = typer.Option(False, "--central", help="List the central ~/.cite/ library."),
) -> None:
    """List references in the library."""
    if central:
        lib = ops.resolve_central()
    else:
        lib = _resolve_library(library)
        _require_initialized(lib)
    _emit(ops.library_view(lib, full=full))


@app.command()
def init(
    library: Path | None = typer.Option(None, help="Library root."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Create without prompting for confirmation."
    ),
) -> None:
    """Create a new cite library at --library (the only way to create one).

    Writes ``cite.toml`` — the marker that distinguishes a real library from an
    arbitrary directory. Every other command that writes to a library (``add``,
    ``add-url``, ``prepare``) *requires* this marker and refuses to create one
    implicitly, so a typo'd ``--library`` path can no longer spin up a stray
    library by accident. Idempotent: re-running against an already-initialized
    library is a no-op and reports ``already_initialized``.

    Creating a *new* library is interactive by default — it prompts for
    confirmation — since it's the one action in this gate that must be a
    deliberate, user-approved choice. Pass --yes to skip the prompt for
    scripted/agent use once the human has approved the location out of band.
    """
    lib = _resolve_library(library)
    # The confirmation prompt is genuinely CLI: ops.init assumes the decision is
    # made. Short-circuit on an already-initialized library so we never prompt for
    # a no-op.
    if lib.is_initialized():
        _emit({"status": "already_initialized", "library": str(lib.root)})
        return
    if not yes:
        if not typer.confirm(f"Create a new cite library at '{lib.root}'?"):
            _emit({"status": "cancelled", "library": str(lib.root)})
            raise typer.Exit(code=1)
    _emit(ops.init(lib))


@app.command()
def doctor(
    library: Path | None = typer.Option(None, help="Library root."),
    self_: bool = typer.Option(
        False, "--self", help="Check the cite install itself instead of the library."
    ),
) -> None:
    """Validate the whole library: bad JSON, missing fields, missing/orphan files.

    With ``--self``, checks the *install* instead: which optional extras are
    present, whether the ``cite-mcp`` entrypoint can start, and which library
    root resolved and how. Run this first whenever results look impossible —
    an empty library, a missing MCP server, an extract command that won't run.
    """
    lib = _resolve_library(library)
    if self_:
        _emit(run_self_check(lib))
        return
    _require_initialized(lib)
    _emit(run_doctor(lib))


@app.command()
def get(
    id: str = typer.Argument(..., help="Record id; bare stem or namespaced (cite:<stem>)."),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Print one reference record."""
    lib = _resolve_library(library)
    _require_initialized(lib)
    result = ops.get(lib, id)
    # A found record is a raw CSL-JSON payload (no spec stamp); the not_found
    # branch is a normal envelope. Distinguish on the sentinel status key.
    if result.get("status") == "not_found":
        _emit(result)
    else:
        _emit(result, raw=True)


@app.command()
def update(
    id: str = typer.Argument(..., help="Record id; bare stem or namespaced (cite:<stem>)."),
    field: list[str] = typer.Option(
        [], "--field", help="key=value to set/replace on the record (repeatable)."
    ),
    remove_field: list[str] = typer.Option(
        [], "--remove-field", help="CSL key to delete from the record (repeatable)."
    ),
    type_: str | None = typer.Option(
        None, "--type", help="Change the cite_type (also rewrites CSL type/genre)."
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Amend a stored reference's metadata in place — the missing CRUD verb.

    Applies the patches (``--field`` sets/replaces, ``--remove-field`` deletes,
    ``--type`` changes the cite_type), re-validates against the type, and rewrites
    the record. The attached document, its content hash, and provenance are
    preserved — only metadata changes, so this is the safe way to fix a wrong field
    without the remove + re-add that would re-file the document.

    When an edit touches an id-bearing field (title, author/editor, or year) the
    deterministic id changes; the whole bundle (directory + every file stem +
    markdown image links) is renamed to match. The content-hash suffix is stable,
    so identity survives the rename. On ``missing_fields`` nothing is committed —
    supply the field and re-run.
    """
    lib = _resolve_library(library)
    try:
        result = ops.update(
            lib, id, fields=field, remove_fields=remove_field, cite_type=type_
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc))
    _emit(result)


@app.command()
def pull(
    id: str = typer.Argument(..., help="Record id in the central library."),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Copy a reference from the central ~/.cite/ library into the project library.

    The explicit cross-project reuse path: browse the central library with
    ``cite list --central``, pick an id, and pull it into the current project.
    The copy is full and independent — the project stays self-contained.
    """
    lib = _resolve_library(library)
    _require_initialized(lib)
    _emit(ops.pull(lib, id))


@app.command()
def remove(
    id: str = typer.Argument(..., help="Record id; bare stem or namespaced (cite:<stem>)."),
    delete_file: bool = typer.Option(
        False, help="Deprecated/no-op: removal now deletes the whole reference bundle."
    ),
    library: Path | None = typer.Option(None, help="Library root."),
    central: bool = typer.Option(
        False, "--central", help="Remove from the central ~/.cite/ library instead."
    ),
) -> None:
    """Remove a reference from the library.

    A reference is a self-contained bundle directory, so removal is all-or-nothing:
    the record, the original file, and any extracted markdown/artifacts go together.
    The legacy ``--delete-file`` flag is accepted but ignored.

    With ``--central``, operates on the central library at ``~/.cite/`` instead of
    the project library. Project removal does NOT affect the central library.
    """
    if central:
        lib = ops.resolve_central()
    else:
        lib = _resolve_library(library)
    _emit(ops.remove(lib, id))


@app.command()
def extract(
    id: str = typer.Argument(
        ..., help="Record id (cite:<stem> or bare stem), or a file path for standalone extraction."
    ),
    engine: str = typer.Option(
        "auto", help="auto | text | vlm — how to read the document."
    ),
    vlm_model: str = typer.Option(
        "granite_docling", help="Docling VLM model preset (used by the vlm engine)."
    ),
    enrich: str = typer.Option(
        "",
        help="formula,code — decode equations/code the text layer mangled (text "
             "engine only; minutes, and a ~600 MB model download on first use).",
    ),
    supplement: int | None = typer.Option(
        None, "--supplement", help="Re-extract the Nth supplementary file instead."
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Extract full markdown for a document using a local Docling pipeline.

    Optional feature — requires the ``extract`` extra (`uv tool install
    'cite[extract]'`). All processing is local/private.

    **Engine.** ``auto`` (the default) probes the document's text layer and picks:
    a born-digital PDF is read through its own text with layout/table models
    (fast, and incapable of misreading text that is already there), while a scan
    goes to the granite-docling vision model. Force one with ``--engine text`` or
    ``--engine vlm``. The chosen engine and the probe are recorded under
    ``_provenance.extraction``. Only the ``vlm`` engine is slow enough to be worth
    backgrounding.

    **Enrichment.** By default docling refuses to guess at an equation it cannot
    read from the text layer, and writes ``<!-- formula-not-decoded -->`` instead
    — so a modelling paper extracts with its equations *missing*, not mangled.
    ``--enrich formula`` re-reads each of those regions with a small vision model
    and emits LaTeX; ``--enrich code`` does the same for code blocks; combine them
    as ``--enrich formula,code``. Off by default because it is slow: a 20-page
    paper with 15 equations took 10 minutes against 12 seconds without it, plus a
    ~600 MB model download on first use. Background it. Text engine only — pass
    ``--engine text`` alongside it, or the request errors on a scanned document.

    **Library mode** (default when ``id`` is a record id): the markdown and its
    referenced images are written into the reference's bundle as
    ``<id>/<id>.md`` + ``<id>/<id>_artifacts/``; re-running overwrites prior
    output. The extractor name + version are recorded under
    ``_provenance.extraction`` so a stale extraction is detectable later.

    **Standalone mode** (when ``id`` is a path to an existing file): no library
    is needed. The markdown and artifacts are written beside the input file as
    ``<stem>.md`` + ``<stem>_artifacts/``.

    **Supplements.** ``--supplement N`` re-extracts one attached supplementary
    file (``<id>/<id>_suppNN.md``) rather than the reference's own document —
    the remedy for a supplement first extracted without ``--enrich formula``.
    """
    if engine not in ("auto", "text", "vlm"):
        raise typer.BadParameter("--engine must be one of: auto, text, vlm")
    enrich = _parse_enrich(enrich)
    candidate = Path(id)
    if candidate.suffix and supplement is None:
        result = ops.extract_file(
            candidate, engine=engine, vlm_model=vlm_model, enrich=enrich
        )
    else:
        lib = _resolve_library(library)
        _require_initialized(lib)
        result = ops.extract(
            lib, id, engine=engine, vlm_model=vlm_model, enrich=enrich,
            supplement=supplement,
        )
    _emit(result)
    if result.get("status") == "error":
        raise typer.Exit(code=1)


@app.command(name="add-supplement")
def add_supplement(
    id: str = typer.Argument(..., help="Parent record id; bare stem or cite:<stem>."),
    file: Path = typer.Argument(..., help="Path to the supplementary file."),
    label: str | None = typer.Option(
        None, help='Human label, e.g. "Supporting Information S1".'
    ),
    engine: str = typer.Option("auto", help="auto | text | vlm — how to read the document."),
    vlm_model: str = typer.Option(
        "granite_docling", help="Docling VLM model preset (used by the vlm engine)."
    ),
    enrich: str = typer.Option(
        "", help="formula,code — decode equations/code the text layer mangled."
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Attach supplementary material to an existing reference and extract its text.

    Supplementary material belongs *to* a paper: it has no citation metadata of
    its own, so it is stored inside the parent's bundle as ``<id>_suppNN.<ext>``
    and listed under ``_provenance.supplements`` — never added as a separate
    reference, and so never appearing in ``list`` or ``export``. It travels with
    the parent through ``update``, ``pull``, central sync and ``remove``.

    Text is extracted where the file type allows: PDFs and office documents go
    through the Docling pipeline (needs the ``extract`` extra), ``.csv``/``.xlsx``
    are converted to markdown tables, and anything else — a ``.zip`` of raw data —
    is stored as-is with no markdown. **A file that cannot be read is still
    attached**, with a ``warning`` on the response.

    Read it back with ``cite text <id> --supplement N``; re-extract it with
    ``cite extract <id> --supplement N``.
    """
    if engine not in ("auto", "text", "vlm"):
        raise typer.BadParameter("--engine must be one of: auto, text, vlm")
    lib = _resolve_library(library)
    _require_initialized(lib)
    result = ops.add_supplement(
        lib, id, file,
        label=label, engine=engine, vlm_model=vlm_model, enrich=_parse_enrich(enrich),
    )
    _emit(result)
    if result.get("status") in ("error", "not_found"):
        raise typer.Exit(code=1)


@app.command()
def text(
    id: str = typer.Argument(..., help="Record id; bare stem or namespaced (cite:<stem>)."),
    path_only: bool = typer.Option(
        False, "--path-only", help="Print the markdown file path instead of its content."
    ),
    supplement: int | None = typer.Option(
        None, "--supplement", help="Print the Nth supplementary file's markdown instead."
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Print a reference's extracted markdown (or its path with --path-only).

    Emits a JSON ``not_found`` envelope if the reference has not been extracted yet.

    ``--supplement N`` prints an attached supplementary file's markdown instead;
    ``cite get <id>`` lists what is attached and under which ordinal.
    """
    lib = _resolve_library(library)
    _require_initialized(lib)
    result = ops.text(lib, id, path_only=path_only, supplement=supplement)
    if result.get("status") == "not_found":
        _emit(result)
        return
    typer.echo(result["path"] if path_only else result["content"])


@app.command()
def export(
    format: str = typer.Option("csl", help="csl | bibtex | pandoc"),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Export the library as CSL-JSON, BibTeX, or Pandoc-ready CSL-JSON."""
    lib = _resolve_library(library)
    _require_initialized(lib)
    try:
        typer.echo(ops.export(lib, format))
    except ValueError as exc:
        raise typer.BadParameter(str(exc))


@app.command()
def guide(json_: bool = typer.Option(False, "--json", help="Emit JSON contract.")) -> None:
    """Print the full agent-facing usage contract."""
    typer.echo(build_guide(as_json=json_))


if __name__ == "__main__":
    app()
