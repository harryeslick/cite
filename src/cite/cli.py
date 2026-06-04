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

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from cite.guide import guide as build_guide
from cite.models import (
    PROVENANCE_KEY,
    Provenance,
    cite_type_from_csl,
    csl_type_for,
    genre_for,
    issued_year,
)
from cite.naming import build_filename, content_hash, record_id
from cite.search import search as run_search
from cite.store import Library
from cite.validate import validate as run_validate
from cite import peek as peek_mod

app = typer.Typer(
    add_completion=False,
    help="A deterministic citation/reference manager for agent use.",
    no_args_is_help=True,
)

# Helper keys the search layer attaches to candidates; we don't persist them.
_HELPER_KEYS = ("source", "source_id", "_cite_type")


# --------------------------------------------------------------------------- #
# Output + library helpers
# --------------------------------------------------------------------------- #


def _emit(obj: Any) -> None:
    """Print a JSON result to stdout (the one channel the agent parses)."""
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False))


def _resolve_library(library: Path | None) -> Library:
    if library is None:
        env = os.environ.get("CITE_LIBRARY")
        library = Path(env) if env else Path("library")
    return Library(library)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _record_from_fields(cite_type: str, fields: list[str]) -> dict:
    """Build a CSL-JSON record from --field key=value pairs (+ type/genre)."""
    record: dict[str, Any] = {"type": csl_type_for(cite_type)}
    genre = genre_for(cite_type)
    if genre:
        record["genre"] = genre

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
# Commands
# --------------------------------------------------------------------------- #


@app.command()
def peek(
    file: Path = typer.Argument(..., exists=True, readable=True),
    max_pages: int = typer.Option(5, help="Pages to scan for a DOI."),
) -> None:
    """Deterministically extract a DOI / title / author guess from a file."""
    _emit(peek_mod.peek(file, max_pages=max_pages))


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
        record = _record_from_fields(type_, field)
        cite_type = type_
    else:
        raise typer.BadParameter("provide one of --doi, --csl, or --manual")

    # Strip search-helper keys so the persisted record is clean CSL.
    for key in _HELPER_KEYS:
        record.pop(key, None)

    # 2. Validate required fields for the type (don't commit if incomplete).
    verdict = run_validate(cite_type, record)
    if verdict["status"] != "ok":
        _emit(verdict)
        return

    # 3. Content hash + dedup gate.
    full_hash = content_hash(file)
    existing = lib.find_by_hash(full_hash)
    if existing:
        prov = existing.get(PROVENANCE_KEY, {})
        _emit({
            "status": "duplicate",
            "message": "identical file already in library",
            "id": prov.get("new_filename", "").rsplit(".", 1)[0],
            "existing": prov,
        })
        return

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
    record[PROVENANCE_KEY] = provenance.model_dump(exclude_none=True)

    # 5. Commit: copy the file in, write the record.
    lib.store_file(file, new_filename)
    lib.write_record(rid, record)

    _emit({
        "status": "added",
        "id": rid,
        "new_filename": new_filename,
        "cite_type": cite_type,
        "record": record,
    })


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
            "id": prov.get("new_filename", "").rsplit(".", 1)[0],
            "cite_type": prov.get("cite_type"),
            "title": r.get("title"),
            "year": issued_year(r),
            "new_filename": prov.get("new_filename"),
        })
    _emit({"count": len(summaries), "references": summaries})


@app.command()
def get(
    id: str = typer.Argument(..., help="Record id (filename stem)."),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Print one reference record."""
    lib = _resolve_library(library)
    try:
        _emit(lib.read_record(id))
    except FileNotFoundError:
        _emit({"status": "not_found", "id": id})


@app.command()
def remove(
    id: str = typer.Argument(..., help="Record id (filename stem)."),
    delete_file: bool = typer.Option(False, help="Also delete the stored file."),
    library: Path | None = typer.Option(None, help="Library root."),
) -> None:
    """Remove a reference (and optionally its file) from the library."""
    lib = _resolve_library(library)
    try:
        lib.remove(id, delete_file=delete_file)
        _emit({"status": "removed", "id": id, "deleted_file": delete_file})
    except FileNotFoundError:
        _emit({"status": "not_found", "id": id})


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
