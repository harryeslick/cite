"""The `cite` command-line interface — the agent-facing orchestrator.

This module is thin glue: it wires the deterministic primitives (peek, search,
naming, validate, store, guide) into a small set of commands and emits every
result as JSON with a ``status`` field. The "smart" decisions (which file,
which candidate, which missing fields to ask the user about) stay with the
agent; everything here is mechanical and reproducible.

Design notes for agents reading this:
  * Every command prints a single JSON object/array to stdout.
  * Commands exit 0 even for ``missing_fields`` or ``duplicate`` — those are
    normal branches of the workflow, not errors. Inspect the ``status`` key.
  * The library root resolves from --library, else $CITE_LIBRARY, else ./library.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util
from pathlib import Path
from typing import Any, Callable

import typer
from slugify import slugify

from cite import __version__
from cite.guide import guide as build_guide
from cite.models import (
    CITE_TYPES,
    PROVENANCE_KEY,
    SPEC_VERSION,
    Extraction,
    Provenance,
    cite_type_from_csl,
    csl_type_for,
    genre_for,
    issued_year,
)
from cite.dedup import find_near_duplicates, find_url_duplicate
from cite.naming import (
    build_filename,
    content_hash,
    local_id,
    namespaced_id,
    record_id,
)
from cite.search import search as run_search
from cite.store import Library
from cite.validate import validate as run_validate
from cite import peek as peek_mod
from cite import web

app = typer.Typer(
    add_completion=False,
    help="A deterministic citation/reference manager for agent use.",
    no_args_is_help=True,
    # Keep Rich's pretty tracebacks ON: a human debugging `cite` at a terminal
    # wants the boxed frames + locals. The MCP wrapper, which can't afford that
    # in a model's context, sets TYPER_STANDARD_TRACEBACK=1 per-invocation so the
    # *same* binary emits a plain one-line traceback there (see cite/mcp.py).
)

# Helper keys the search layer attaches to candidates; we don't persist them.
_HELPER_KEYS = ("source", "source_id", "_cite_type")


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
    if library is None:
        env = os.environ.get("CITE_LIBRARY")
        library = Path(env) if env else Path("library")
    return Library(library)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_date_parts() -> dict:
    """Today's date as a CSL date object — the `accessed` value for a web add."""
    now = datetime.now(timezone.utc)
    return {"date-parts": [[now.year, now.month, now.day]]}


# --------------------------------------------------------------------------- #
# Manual / CSL record construction
# --------------------------------------------------------------------------- #


def _parse_date(value: str) -> dict:
    """Parse 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' into a CSL date object."""
    parts = [int(p) for p in value.strip().split("-") if p]
    return {"date-parts": [parts]} if parts else {}


def _parse_authors(value: str) -> list[dict]:
    """Parse 'Smith, John; Lee, Kim' into CSL name objects.

    A segment without a comma is treated as a literal (organisational) name,
    e.g. 'World Health Organization'.
    """
    authors: list[dict] = []
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "," in chunk:
            family, given = (s.strip() for s in chunk.split(",", 1))
            authors.append({"family": family, "given": given})
        else:
            authors.append({"literal": chunk})
    return authors


def _apply_fields(record: dict[str, Any], fields: list[str]) -> None:
    """Apply --field key=value pairs onto an existing record (in place).

    The single place the ``key=value`` mini-language is interpreted, so `add
    --manual` (building a record from scratch) and `update --field` (patching a
    stored one) parse identically: ``author``/``editor`` expand to CSL name
    objects, the date keys (``issued``/``accessed``, plus ``year`` as an alias
    for ``issued``) to CSL date objects, everything else is a literal string.
    """
    for raw in fields:
        if "=" not in raw:
            raise typer.BadParameter(f"--field must be key=value, got {raw!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if key in ("author", "editor"):
            record[key] = _parse_authors(value)
        elif key in ("issued", "accessed", "year"):
            record["issued" if key == "year" else key] = _parse_date(value)
        else:
            record[key] = value


def _record_from_fields(cite_type: str, fields: list[str]) -> dict:
    """Build a CSL-JSON record from --field key=value pairs (+ type/genre)."""
    record: dict[str, Any] = {"type": csl_type_for(cite_type)}
    genre = genre_for(cite_type)
    if genre:
        record["genre"] = genre
    _apply_fields(record, fields)
    return record


def _load_csl(source: str) -> dict:
    """Load a single CSL-JSON record from a file path or '-' (stdin)."""
    text = sys.stdin.read() if source == "-" else Path(source).read_text()
    data = json.loads(text)
    if isinstance(data, dict) and "candidates" in data:
        raise typer.BadParameter(
            "received a search result, not a single record; "
            "pipe one candidate from the 'candidates' list"
        )
    if not isinstance(data, dict):
        raise typer.BadParameter("CSL input must be a single JSON object")
    return data


# --------------------------------------------------------------------------- #
# Shared commit pipeline (used by both `add` and `add-url`)
# --------------------------------------------------------------------------- #


def _commit_file(
    lib: Library,
    file: Path,
    record: dict,
    cite_type: str,
    *,
    source: str,
    source_id: str | None,
    force: bool,
    manual: bool = False,
    dup_finder: Callable[[dict, list[dict]], list] = find_near_duplicates,
) -> dict:
    """Validate, dedup, hash, rename, and store a record + its file. Return an envelope.

    The file-agnostic back half of the add pipeline, shared so `add` (local file)
    and `add-url` (fetched snapshot) commit identically. Returns the response dict
    (the caller emits it); commits nothing when validation or a dedup gate stops it.

    `dup_finder` selects the near-duplicate gate: the default DOI/title/author/year
    matcher for documents, or `find_url_duplicate` (exact-URL only) for the web path.
    Both return the same `Match` shape, so the `near_duplicate` envelope is uniform.
    """
    # Strip search-helper keys so the persisted record is clean CSL.
    for key in _HELPER_KEYS:
        record.pop(key, None)

    # 1. Validate required fields for the type (don't commit if incomplete).
    verdict = run_validate(cite_type, record)
    if verdict["status"] != "ok":
        # On a manual add we still hold the source file, and its embedded metadata
        # often supplies exactly the fields that are missing. Point back at peek so
        # the agent recovers them before asking the user (and never fabricates).
        if manual and verdict["status"] == "missing_fields":
            verdict["suggested_next"] = (
                f"recover the missing fields from the document before asking the "
                f"user: cite peek {file}"
            )
        return verdict

    # 2. Content hash + exact-bytes dedup gate (absolute; --force never skips it).
    full_hash = content_hash(file)
    existing = lib.find_by_hash(full_hash)
    if existing:
        prov = existing.get(PROVENANCE_KEY, {})
        return {
            "status": "duplicate",
            "message": "identical file already in library",
            "id": namespaced_id(prov.get("new_filename", "").rsplit(".", 1)[0]),
            "existing": prov,
        }

    # 3. Near-duplicate gate: catch the *same work* under different bytes (same DOI/
    # title/author/year for documents, or same URL for the web path). Skipped with
    # --force. Commits nothing on a hit — the agent decides or asks the user, then
    # re-adds with --force.
    if not force:
        candidates = dup_finder(record, lib.list_records())
        if candidates:
            top = candidates[0].tier
            return {
                "status": "near_duplicate",
                "message": (
                    f"{len(candidates)} possible match(es) — review before adding "
                    f"(strongest tier: {top})"
                ),
                "candidates": [c.to_dict() for c in candidates],
                "resolution": "if genuinely new, re-run with --force",
            }

    # 4. Deterministic rename + provenance.
    ext = file.suffix.lstrip(".")
    new_filename = build_filename(record, full_hash, ext)
    rid = record_id(record, full_hash)
    provenance = Provenance(
        cite_type=cite_type,
        original_filename=file.name,
        new_filename=new_filename,
        date_added=_now_iso(),
        file_hash=full_hash,
        source=source,
        source_id=source_id,
    )
    prov_dict = provenance.model_dump(exclude_none=True)

    # 5. Commit: copy the file into the reference's bundle, write the record.
    lib.store_file(file, rid, new_filename)

    # 5b. Adopt any pre-add extraction staged by `cite prepare` for these exact
    # bytes (no-op if nothing was staged), so the expensive VLM pass never repeats.
    adopted = lib.adopt_staged(full_hash, rid)
    if adopted:
        prov_dict["extraction"] = Extraction(**adopted).model_dump(exclude_none=True)

    record[PROVENANCE_KEY] = prov_dict
    lib.write_record(rid, record)

    return {
        "status": "added",
        "id": namespaced_id(rid),
        "new_filename": new_filename,
        "cite_type": cite_type,
        "extracted": adopted is not None,
        "markdown_path": adopted["markdown_path"] if adopted else None,
        "record": record,
    }


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


# Maps each optional extra (declared in pyproject's [project.optional-dependencies])
# to the import name that proves it is actually installed. We probe the *module*
# rather than `importlib.metadata` because what callers care about is "can the
# feature run", and probing keeps it cheap: `find_spec` only locates the module on
# sys.path, it never imports it — so checking `extract` does not drag in docling/torch.
_EXTRA_PROBES = {"mcp": "mcp", "extract": "docling"}


def _installed_extras() -> dict[str, bool]:
    """Report which optional extras are available, e.g. {'mcp': True, 'extract': False}."""
    return {
        extra: importlib_util.find_spec(module) is not None
        for extra, module in _EXTRA_PROBES.items()
    }


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
            "extras": _installed_extras(),
        }
    )


@app.command()
def peek(
    file: Path = typer.Argument(..., exists=True, readable=True),
    max_pages: int = typer.Option(5, help="Pages to scan for a DOI."),
) -> None:
    """Deterministically extract a DOI / title / author guess from a file."""
    _emit(peek_mod.peek(file, max_pages=max_pages))


def _markdown_head(text: str, *, max_chars: int) -> tuple[str, bool]:
    """Return the leading ``max_chars`` of markdown and whether it was truncated.

    Truncates on a line boundary so the head never ends mid-line (keeps the
    title/author block the agent reads intact and the output context-frugal).
    """
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut, True


def _first_heading(text: str) -> str | None:
    """First Markdown ATX heading (``# ...``) — a decent title guess for a report."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    return None


@app.command()
def prepare(
    file: Path = typer.Argument(..., exists=True, readable=True),
    head_chars: int = typer.Option(
        2000, help="How many leading characters of the extracted markdown to return."
    ),
    vlm_model: str = typer.Option(
        "granite_docling", help="Docling VLM model preset to use."
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Extract a file's full markdown *before* adding it, for better citation context.

    The recommended first step when the ``extract`` extra is installed — especially
    for reports with no DOI and thin embedded metadata, where the document text
    itself is the best source of title / author / publisher / date. It runs the
    local Docling VLM once, caches the markdown under the file's content hash in
    ``<library>/.staging/<hash>/``, and returns the markdown *head* plus any DOI /
    title it can read from it. Read the head, then ``cite search`` (now better
    informed) or build a manual record; the later ``cite add <file>`` recomputes
    the same hash and adopts this cached extraction, so the VLM never runs twice.

    Degrades cleanly: if the ``extract`` extra is not installed it returns
    ``status: extractor_unavailable`` pointing you at the deterministic fallback
    (``cite peek``) rather than erroring out. Slow (a vision model runs over the
    whole document); if your runtime can background shell commands, run it detached.
    """
    lib = _resolve_library(library)
    lib.init()

    full_hash = content_hash(file)

    # Already filed? Don't burn minutes re-extracting something in the library.
    existing = lib.find_by_hash(full_hash)
    if existing:
        prov = existing.get(PROVENANCE_KEY, {})
        stem = prov.get("new_filename", "").rsplit(".", 1)[0]
        _emit({
            "status": "duplicate",
            "message": "identical file already in library — no need to prepare",
            "id": namespaced_id(stem),
            "hint": f"read its extracted text with `cite text {namespaced_id(stem)}`",
        })
        return

    # Reuse a prior staged extraction (idempotent re-call) instead of re-running.
    if lib.has_staged(full_hash):
        meta = lib.read_staged_meta(full_hash)
    else:
        from cite.extract import ExtractorUnavailable, extract_to_markdown

        staged_md = lib.staging_text_path(full_hash)
        lib.clear_staged(full_hash)  # wipe any partial/stale staging first
        try:
            result = extract_to_markdown(file, staged_md, vlm_model=vlm_model)
        except ExtractorUnavailable:
            lib.clear_staged(full_hash)
            _emit({
                "status": "extractor_unavailable",
                "message": "the 'extract' extra (Docling) is not installed",
                "hint": "install it with `uv tool install 'cite[extract]'`, or use "
                        "the deterministic fallback below",
                "suggested_next": f"cite peek {file}",
            })
            return
        except Exception as e:  # docling runtime failure — surface, don't crash
            lib.clear_staged(full_hash)
            _emit({"status": "error", "message": f"extraction failed: {e}"})
            raise typer.Exit(code=1)

        meta = {
            "extractor": result["extractor"],
            "extractor_version": result["extractor_version"],
            "vlm_model": result["vlm_model"],
            "image_export_mode": result["image_export_mode"],
            "extracted_at": _now_iso(),
            "source_file_hash": full_hash,
            "n_images": result["n_images"],
        }
        lib.write_staged_meta(full_hash, meta)

    text = lib.staging_text_path(full_hash).read_text(encoding="utf-8")
    head, truncated = _markdown_head(text, max_chars=head_chars)

    # Re-scan the *extracted* text for a DOI — far richer than pypdf on scanned
    # reports — and take the first heading as a title guess.
    doi_match = peek_mod._DOI_RE.search(text)
    doi = peek_mod._clean_doi(doi_match.group(0)) if doi_match else None
    title_guess = _first_heading(text)

    if doi:
        suggested_next = f"cite search --doi {doi}"
    elif title_guess:
        suggested_next = f'cite search --title "{title_guess}"'
    else:
        suggested_next = (
            "read the head to identify title/author/publisher/date, then "
            "`cite search` or `cite add ... --manual`"
        )

    _emit({
        "status": "staged",
        "file": file.name,
        "file_hash": full_hash,
        "markdown_path": str(lib.staging_text_path(full_hash)),
        "head": head,
        "head_truncated": truncated,
        "n_images": meta.get("n_images", 0),
        "doi": doi,
        "title_guess": title_guess,
        "suggested_next": suggested_next,
        "note": "after you pick metadata, `cite add <file> ...` adopts this "
                "extraction into the bundle (no re-extract).",
    })


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
    lib.init()

    # 1. Assemble the base CSL record and figure out source/cite_type.
    source = "manual"
    source_id: str | None = None

    if doi:
        result = run_search(doi=doi)
        if result["status"] != "ok":
            _emit({
                "status": "not_found",
                "doi": doi,
                "hint": "no DB match; add manually: cite add <file> --manual "
                        "--type <type> --field key=val ...",
            })
            return
        record = result["candidates"][0]
        cite_type = record.get("_cite_type") or "other-report"
        source = record.get("source", "manual")
        source_id = record.get("source_id")
    elif csl is not None:
        record = _load_csl(csl)
        source = record.get("source", "manual")
        source_id = record.get("source_id") or record.get("DOI")
        cite_type = record.get("_cite_type") or cite_type_from_csl(
            record.get("type"), record.get("genre")
        )
    elif manual:
        if not type_:
            raise typer.BadParameter("--manual requires --type")
        try:
            record = _record_from_fields(type_, field)
        except ValueError as exc:
            # An unknown cite_type (or other field-construction problem) is agent
            # error, not a bug — return the one-line reason as a structured
            # envelope rather than letting it escape as a traceback.
            _emit({"status": "error", "message": str(exc)})
            return
        cite_type = type_
    else:
        raise typer.BadParameter("provide one of --doi, --csl, or --manual")

    # 2-5. Validate, dedup, hash, rename, and commit (shared with `add-url`).
    _emit(_commit_file(
        lib, file, record, cite_type,
        source=source, source_id=source_id, force=force, manual=manual,
    ))


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
    lib.init()

    try:
        fetched = web.fetch_url(url)
    except web.WebFetchError as exc:
        _emit({"status": "fetch_failed", "url": url, "message": exc.message})
        return

    # PDF branch: download + peek, then defer to the existing document workflow.
    if web.is_pdf(fetched):
        _emit(_download_for_add(lib, fetched))
        return

    if not web.is_html(fetched):
        _emit({
            "status": "unsupported_content_type",
            "url": fetched.final_url,
            "content_type": fetched.content_type,
            "hint": "cite add-url handles HTML pages and PDFs; for other documents, "
                    "download the file yourself and use `cite add <file>`",
        })
        return

    # HTML branch: parse metadata, snapshot the bytes, commit as a web-site.
    record = web.parse_webpage_metadata(fetched.text, fetched.final_url)
    record["accessed"] = _today_date_parts()

    # Write the fetched bytes to a temp file so the shared pipeline can hash/store
    # them; the snapshot is archived in the bundle as `<id>.html`. Name the temp
    # file after the URL (not a random temp stem) so `_provenance.original_filename`
    # stays meaningful — the URL is the "original" for a web add.
    tmpdir = Path(tempfile.mkdtemp())
    try:
        name = slugify(fetched.final_url, max_length=60) or "webpage"
        snapshot = tmpdir / f"{name}.html"
        snapshot.write_bytes(fetched.body)
        result = _commit_file(
            lib, snapshot, record, "web-site",
            source="web", source_id=fetched.final_url, force=force,
            dup_finder=find_url_duplicate,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    _emit(result)


def _download_for_add(lib: Library, fetched: web.Fetched) -> dict:
    """Save a fetched PDF to the library's `.downloads/` and peek it.

    Stops short of adding: resolving a document's citation needs a DOI/title search
    whose candidate the agent must choose, so we hand back the saved path plus a
    peek (any embedded DOI/title) and point at the normal `search` -> `add` flow.
    """
    digest = hashlib.sha256(fetched.body).hexdigest()
    downloads = lib.root / ".downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    dest = downloads / f"{digest[:16]}.pdf"
    dest.write_bytes(fetched.body)

    peeked = peek_mod.peek(dest)
    doi = peeked.get("doi")
    if doi:
        suggested = f"cite search --doi {doi}, then cite add {dest} --csl -"
    else:
        suggested = (
            f"cite peek {dest} / read it for a title, then cite search and "
            f"cite add {dest} --csl -"
        )
    return {
        "status": "downloaded",
        "kind": "pdf",
        "path": str(dest),
        "source_url": fetched.final_url,
        "peek": peeked,
        "suggested_next": suggested,
        "note": "PDF saved, not yet added — continue with the normal document flow.",
    }


@app.command()
def validate(
    type_: str = typer.Option(..., "--type", help="cite_type to validate against."),
    csl: str = typer.Option("-", help="CSL-JSON record path, or '-' for stdin."),
) -> None:
    """Check a record against its type's required fields; list what's missing."""
    _emit(run_validate(type_, _load_csl(csl)))


@app.command(name="list")
def list_(
    library: Path | None = typer.Option(None, help="Library root."),
    full: bool = typer.Option(False, help="Emit full records instead of summaries."),
) -> None:
    """List references in the library."""
    lib = _resolve_library(library)
    records = lib.list_records()
    if full:
        _emit(records)
        return
    summaries = []
    for r in records:
        prov = r.get(PROVENANCE_KEY, {})
        summaries.append({
            "id": namespaced_id(prov.get("new_filename", "").rsplit(".", 1)[0]),
            "cite_type": prov.get("cite_type"),
            "title": r.get("title"),
            "year": issued_year(r),
            "new_filename": prov.get("new_filename"),
        })
    _emit({"count": len(summaries), "references": summaries})


@app.command()
def get(
    id: str = typer.Argument(..., help="Record id; bare stem or namespaced (cite:<stem>)."),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Print one reference record."""
    lib = _resolve_library(library)
    stem = local_id(id)
    try:
        # The record itself is a raw CSL-JSON payload, not an envelope — emit it
        # untouched (no spec stamp); the spec belongs to control responses.
        _emit(lib.read_record(stem), raw=True)
    except FileNotFoundError:
        _emit({"status": "not_found", "id": namespaced_id(stem)})


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
    stem = local_id(id)
    try:
        record = lib.read_record(stem)
    except FileNotFoundError:
        _emit({"status": "not_found", "id": namespaced_id(stem)})
        return

    if not field and not remove_field and not type_:
        _emit({
            "status": "error",
            "id": namespaced_id(stem),
            "message": "no changes given; pass --field, --remove-field, or --type",
        })
        return

    prov = record.get(PROVENANCE_KEY, {})
    cite_type = type_ or prov.get("cite_type")
    if cite_type not in CITE_TYPES:
        _emit({
            "status": "error",
            "id": namespaced_id(stem),
            "message": f"unknown cite_type {cite_type!r}; must be one of "
                       f"{', '.join(CITE_TYPES)}",
        })
        return

    # A type change rewrites the CSL `type`/`genre` (and the stored cite_type).
    if type_:
        record["type"] = csl_type_for(type_)
        genre = genre_for(type_)
        if genre:
            record["genre"] = genre
        else:
            record.pop("genre", None)
        prov["cite_type"] = type_

    # Deletes first, then sets — so `--remove-field x --field x=...` ends up set.
    for key in remove_field:
        record.pop(key.strip(), None)
    _apply_fields(record, field)

    # Re-validate the patched record; commit nothing if it's now incomplete.
    verdict = run_validate(cite_type, record)
    if verdict["status"] != "ok":
        verdict["id"] = namespaced_id(stem)
        _emit(verdict)
        return

    # Recompute the id from the patched metadata + the (unchanged) content hash.
    full_hash = prov.get("file_hash")
    new_stem = record_id(record, full_hash) if full_hash else stem
    renamed = new_stem != stem
    if renamed:
        try:
            new_doc = lib.rename_bundle(stem, new_stem)
        except FileExistsError:
            _emit({
                "status": "error",
                "id": namespaced_id(stem),
                "message": f"target id {namespaced_id(new_stem)} already exists; "
                           "resolve the clash before updating",
            })
            return
        if new_doc:
            prov["new_filename"] = new_doc
        extraction = prov.get("extraction")
        if isinstance(extraction, dict) and extraction.get("markdown_path"):
            extraction["markdown_path"] = f"{new_stem}/{new_stem}.md"

    record[PROVENANCE_KEY] = prov
    lib.write_record(new_stem, record)

    _emit({
        "status": "updated",
        "id": namespaced_id(new_stem),
        "renamed": renamed,
        "old_id": namespaced_id(stem) if renamed else None,
        "cite_type": cite_type,
        "new_filename": prov.get("new_filename"),
        "record": record,
    })


@app.command()
def remove(
    id: str = typer.Argument(..., help="Record id; bare stem or namespaced (cite:<stem>)."),
    delete_file: bool = typer.Option(
        False, help="Deprecated/no-op: removal now deletes the whole reference bundle."
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Remove a reference from the library.

    A reference is a self-contained bundle directory, so removal is all-or-nothing:
    the record, the original file, and any extracted markdown/artifacts go together.
    The legacy ``--delete-file`` flag is accepted but ignored.
    """
    lib = _resolve_library(library)
    stem = local_id(id)
    try:
        lib.remove(stem)
        _emit({
            "status": "removed",
            "id": namespaced_id(stem),
            "deleted_file": True,
        })
    except FileNotFoundError:
        _emit({"status": "not_found", "id": namespaced_id(stem)})


@app.command()
def extract(
    id: str = typer.Argument(..., help="Record id; bare stem or namespaced (cite:<stem>)."),
    vlm_model: str = typer.Option(
        "granite_docling", help="Docling VLM model preset to use."
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Extract full markdown for a stored reference using a local Docling VLM.

    Optional feature — requires the ``extract`` extra (`uv tool install
    'cite[extract]'`). All processing is local/private. The markdown and its
    referenced images are written into the reference's bundle as
    ``<id>/<id>.md`` + ``<id>/<id>_artifacts/``; re-running overwrites prior
    output. The extractor name + version are recorded under
    ``_provenance.extraction`` so a stale extraction is detectable later.
    """
    # Lazy import: cite.extract never pulls docling at import time, but keep the
    # CLI's startup path clear of the extraction facade until it's actually used.
    from cite.extract import ExtractorUnavailable, extract_to_markdown

    lib = _resolve_library(library)
    stem = local_id(id)
    try:
        record = lib.read_record(stem)
    except FileNotFoundError:
        _emit({"status": "not_found", "id": namespaced_id(stem)})
        return

    prov = record.get(PROVENANCE_KEY, {})
    new_filename = prov.get("new_filename")
    src = lib.entry_dir(stem) / new_filename if new_filename else None
    if src is None or not src.exists():
        _emit({
            "status": "error",
            "id": namespaced_id(stem),
            "message": "stored source file not found for this reference",
        })
        raise typer.Exit(code=1)

    lib.clear_text(stem)  # idempotent re-extract: wipe any prior output first
    try:
        result = extract_to_markdown(src, lib.text_path(stem), vlm_model=vlm_model)
    except ExtractorUnavailable as e:
        _emit({"status": "error", "message": str(e), "hint": "install cite[extract]"})
        raise typer.Exit(code=1)
    except Exception as e:  # docling runtime failure — surface, don't crash
        _emit({"status": "error", "message": f"extraction failed: {e}"})
        raise typer.Exit(code=1)

    md_rel = f"{stem}/{stem}.md"
    extraction = Extraction(
        extractor=result["extractor"],
        extractor_version=result["extractor_version"],
        vlm_model=result["vlm_model"],
        image_export_mode=result["image_export_mode"],
        extracted_at=_now_iso(),
        source_file_hash=prov.get("file_hash") or content_hash(src),
        markdown_path=md_rel,
        n_images=result["n_images"],
    )
    prov["extraction"] = extraction.model_dump(exclude_none=True)
    record[PROVENANCE_KEY] = prov
    lib.write_record(stem, record)

    _emit({
        "status": "ok",
        "id": namespaced_id(stem),
        "markdown_path": md_rel,
        "n_images": result["n_images"],
        "extractor": result["extractor"],
        "extractor_version": result["extractor_version"],
    })


@app.command()
def text(
    id: str = typer.Argument(..., help="Record id; bare stem or namespaced (cite:<stem>)."),
    path_only: bool = typer.Option(
        False, "--path-only", help="Print the markdown file path instead of its content."
    ),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Print a reference's extracted markdown (or its path with --path-only).

    Emits a JSON ``not_found`` envelope if the reference has not been extracted yet.
    """
    lib = _resolve_library(library)
    stem = local_id(id)
    if not lib.text_path(stem).exists():
        _emit({
            "status": "not_found",
            "id": namespaced_id(stem),
            "hint": "run `cite extract <id>` first (needs the 'extract' extra)",
        })
        return
    typer.echo(str(lib.text_path(stem)) if path_only else lib.read_text(stem))


@app.command()
def export(
    format: str = typer.Option("csl", help="csl | bibtex | pandoc"),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Export the library as CSL-JSON, BibTeX, or Pandoc-ready CSL-JSON."""
    lib = _resolve_library(library)
    records = lib.list_records()
    fmt = format.lower()
    if fmt in ("csl", "pandoc"):
        # Pandoc --citeproc consumes CSL-JSON directly.
        typer.echo(json.dumps(records, indent=2, ensure_ascii=False))
    elif fmt == "bibtex":
        typer.echo("\n\n".join(_to_bibtex(r) for r in records))
    else:
        raise typer.BadParameter("format must be csl, bibtex, or pandoc")


@app.command()
def guide(json_: bool = typer.Option(False, "--json", help="Emit JSON contract.")) -> None:
    """Print the full agent-facing usage contract."""
    typer.echo(build_guide(as_json=json_))


# --------------------------------------------------------------------------- #
# Minimal CSL -> BibTeX (leans on CSL being a standard)
# --------------------------------------------------------------------------- #

_BIBTEX_TYPE = {
    "article-journal": "article",
    "book": "book",
    "chapter": "incollection",
    "report": "techreport",
    "dataset": "misc",
    "webpage": "online",
}


def _bibtex_authors(record: dict) -> str:
    names = []
    for a in record.get("author", []):
        if "literal" in a:
            names.append(a["literal"])
        else:
            names.append(", ".join(p for p in (a.get("family"), a.get("given")) if p))
    return " and ".join(names)


def _to_bibtex(record: dict) -> str:
    prov = record.get(PROVENANCE_KEY, {})
    key = prov.get("new_filename", "ref").rsplit(".", 1)[0] or "ref"
    entry = _BIBTEX_TYPE.get(record.get("type", ""), "misc")
    lines = [f"@{entry}{{{key},"]
    fields = {
        "title": record.get("title"),
        "author": _bibtex_authors(record) or None,
        "journal": record.get("container-title")
        if entry in ("article", "incollection")
        else None,
        "year": issued_year(record),
        "publisher": record.get("publisher"),
        "doi": record.get("DOI"),
        "url": record.get("URL"),
    }
    for k, v in fields.items():
        if v:
            lines.append(f"  {k} = {{{v}}},")
    lines.append("}")
    return "\n".join(lines)


if __name__ == "__main__":
    app()
