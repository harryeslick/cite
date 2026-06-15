"""Orchestration core for ``cite`` — plain args in, envelope dict out.

This module owns every deterministic operation the tool performs (add, prepare,
update, extract, url, …) *without* any I/O or framework coupling: no ``typer``,
no ``print``, no ``sys.stdin``, no spec stamp. Each function takes plain Python
arguments and returns a naked envelope ``dict`` (the ``{"status": ...}`` shape).

Both entry points are thin shells over this core:
  * ``cite.cli`` parses argv, reads stdin/files, prompts, then ``_emit``s the dict.
  * ``cite.mcp`` calls the same functions in-process and ``json.dumps``es the dict.

The suite spec stamp (``SPEC_VERSION``) and JSON serialisation belong to those
shells — *not* here. ops returns the dict; the caller stamps and serialises it.

Error convention: most "errors" are ordinary returned dicts (``not_found``,
``duplicate``, ``near_duplicate``, ``missing_fields``, ``already_initialized``) —
branches of the workflow, not exceptions. Only genuinely bad *input* raises
``ValueError`` (the shell decides how to surface it); a docling runtime crash
propagates as a generic ``Exception`` for the shell to wrap.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from slugify import slugify

from cite import peek as peek_mod
from cite import web
from cite.dedup import find_near_duplicates, find_url_duplicate
from cite.models import (
    CITE_TYPES,
    PROVENANCE_KEY,
    Extraction,
    Provenance,
    cite_type_from_csl,
    csl_type_for,
    genre_for,
    issued_year,
)
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

# Helper keys the search layer attaches to candidates; we don't persist them.
_HELPER_KEYS = ("source", "source_id", "_cite_type")


# --------------------------------------------------------------------------- #
# Library + time helpers
# --------------------------------------------------------------------------- #


def resolve_library(library: Path | None) -> Library:
    """Resolve the library root: explicit --library, else $CITE_LIBRARY, else ./library."""
    if library is None:
        env = os.environ.get("CITE_LIBRARY")
        library = Path(env) if env else Path("library")
    return Library(library)


def require_initialized(lib: Library) -> dict | None:
    """Return an error envelope if ``lib`` isn't a real library yet, else None.

    ``cite.toml`` is the sole marker of an initialized library; creating one is a
    deliberate act reserved for ``init`` (never implicit on add). The shell emits
    the returned dict and stops; ``None`` means "proceed".
    """
    if not lib.is_initialized():
        return {
            "status": "error",
            "message": f"no cite library at '{lib.root}' (missing cite.toml)",
            "hint": f"run: cite init --library {lib.root}",
        }
    return None


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


def apply_fields(record: dict[str, Any], fields: list[str]) -> None:
    """Apply --field key=value pairs onto an existing record (in place).

    The single place the ``key=value`` mini-language is interpreted, so building a
    record from scratch and patching a stored one parse identically:
    ``author``/``editor`` expand to CSL name objects, the date keys
    (``issued``/``accessed``, plus ``year`` as an alias for ``issued``) to CSL date
    objects, everything else is a literal string. Raises ``ValueError`` on a
    malformed entry (no ``=``).
    """
    for raw in fields:
        if "=" not in raw:
            raise ValueError(f"--field must be key=value, got {raw!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if key in ("author", "editor"):
            record[key] = _parse_authors(value)
        elif key in ("issued", "accessed", "year"):
            record["issued" if key == "year" else key] = _parse_date(value)
        else:
            record[key] = value


def record_from_fields(cite_type: str, fields: list[str]) -> dict:
    """Build a CSL-JSON record from --field key=value pairs (+ type/genre).

    Raises ``ValueError`` for an unknown ``cite_type`` (via ``csl_type_for``) or a
    malformed field.
    """
    record: dict[str, Any] = {"type": csl_type_for(cite_type)}
    genre = genre_for(cite_type)
    if genre:
        record["genre"] = genre
    apply_fields(record, fields)
    return record


def load_csl(text: str) -> dict:
    """Parse a single CSL-JSON record from already-read text.

    The caller reads stdin / a file and hands the raw text here. Raises
    ``ValueError`` if it's a search result, not an object, or invalid JSON
    (``json.JSONDecodeError`` is a subclass of ``ValueError``).
    """
    data = json.loads(text)
    if isinstance(data, dict) and "candidates" in data:
        raise ValueError(
            "received a search result, not a single record; "
            "pipe one candidate from the 'candidates' list"
        )
    if not isinstance(data, dict):
        raise ValueError("CSL input must be a single JSON object")
    return data


# --------------------------------------------------------------------------- #
# Shared commit pipeline (used by both `add` and `add_url`)
# --------------------------------------------------------------------------- #


def commit_file(
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
    and `add_url` (fetched snapshot) commit identically. Returns the response dict;
    commits nothing when validation or a dedup gate stops it.

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

    # 5b. Adopt any pre-add extraction staged by `prepare` for these exact bytes
    # (no-op if nothing was staged), so the expensive VLM pass never repeats.
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
# Add operations — one per deterministic op (see mcp.py tool surface)
# --------------------------------------------------------------------------- #


def add_by_doi(
    lib: Library, file: Path, doi: str, *, force: bool = False
) -> dict:
    """Fetch a citation by DOI, then validate/hash/rename/store it.

    Returns ``not_found`` if no DB match (the agent falls back to manual).
    """
    result = run_search(doi=doi)
    if result["status"] != "ok":
        return {
            "status": "not_found",
            "doi": doi,
            "hint": "no DB match; add manually: cite add <file> --manual "
                    "--type <type> --field key=val ...",
        }
    record = result["candidates"][0]
    cite_type = record.get("_cite_type") or "other-report"
    source = record.get("source", "manual")
    source_id = record.get("source_id")
    return commit_file(
        lib, file, record, cite_type,
        source=source, source_id=source_id, force=force,
    )


def add_from_csl(
    lib: Library, file: Path, record: dict, *, force: bool = False
) -> dict:
    """Add a file using a caller-supplied CSL-JSON record (e.g. a search candidate)."""
    source = record.get("source", "manual")
    source_id = record.get("source_id") or record.get("DOI")
    cite_type = record.get("_cite_type") or cite_type_from_csl(
        record.get("type"), record.get("genre")
    )
    return commit_file(
        lib, file, record, cite_type,
        source=source, source_id=source_id, force=force,
    )


def add_manual(
    lib: Library,
    file: Path,
    cite_type: str,
    fields: list[str],
    *,
    force: bool = False,
) -> dict:
    """Build a record from --field values and add it. Raises ``ValueError`` on a
    bad cite_type / field (the shell surfaces it as an error envelope)."""
    record = record_from_fields(cite_type, fields)
    return commit_file(
        lib, file, record, cite_type,
        source="manual", source_id=None, force=force, manual=True,
    )


def add_url(lib: Library, url: str, *, force: bool = False) -> dict:
    """Add a citation from a URL: fetch the page, snapshot it, read its metadata.

    Branches on the response Content-Type: an HTML page is archived as the
    reference's document and parsed into a ``web-site`` record; a PDF is downloaded
    and peeked, handed back with status ``downloaded`` for the normal document flow.
    Returns ``fetch_failed`` on a ``WebFetchError`` (no exception escapes).
    """
    try:
        fetched = web.fetch_url(url)
    except web.WebFetchError as exc:
        return {"status": "fetch_failed", "url": url, "message": exc.message}

    # PDF branch: download + peek, then defer to the existing document workflow.
    if web.is_pdf(fetched):
        return download_for_add(lib, fetched)

    if not web.is_html(fetched):
        return {
            "status": "unsupported_content_type",
            "url": fetched.final_url,
            "content_type": fetched.content_type,
            "hint": "cite add-url handles HTML pages and PDFs; for other documents, "
                    "download the file yourself and use `cite add <file>`",
        }

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
        return commit_file(
            lib, snapshot, record, "web-site",
            source="web", source_id=fetched.final_url, force=force,
            dup_finder=find_url_duplicate,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def download_for_add(lib: Library, fetched: web.Fetched) -> dict:
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


# --------------------------------------------------------------------------- #
# Prepare (pre-add extraction)
# --------------------------------------------------------------------------- #


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


def prepare(
    lib: Library,
    file: Path,
    *,
    head_chars: int = 2000,
    vlm_model: str = "granite_docling",
) -> dict:
    """Extract a file's full markdown *before* adding it, for better citation context.

    Runs the local Docling VLM once, caches the markdown under the file's content
    hash in ``<library>/.staging/<hash>/``, and returns the markdown *head* plus any
    DOI / title it can read. Returns ``duplicate`` if already filed,
    ``extractor_unavailable`` if the extra isn't installed. A docling runtime crash
    propagates as a generic ``Exception`` for the shell to wrap.
    """
    full_hash = content_hash(file)

    # Already filed? Don't burn minutes re-extracting something in the library.
    existing = lib.find_by_hash(full_hash)
    if existing:
        prov = existing.get(PROVENANCE_KEY, {})
        stem = prov.get("new_filename", "").rsplit(".", 1)[0]
        return {
            "status": "duplicate",
            "message": "identical file already in library — no need to prepare",
            "id": namespaced_id(stem),
            "hint": f"read its extracted text with `cite text {namespaced_id(stem)}`",
        }

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
            return {
                "status": "extractor_unavailable",
                "message": "the 'extract' extra (Docling) is not installed",
                "hint": "install it with `uv tool install 'cite[extract]'`, or use "
                        "the deterministic fallback below",
                "suggested_next": f"cite peek {file}",
            }
        except Exception:  # docling runtime failure — let the shell wrap it
            lib.clear_staged(full_hash)
            raise

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

    return {
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
    }


# --------------------------------------------------------------------------- #
# Library management operations
# --------------------------------------------------------------------------- #


def init(lib: Library) -> dict:
    """Create a new cite library — assumes the decision to create has been made.

    The shell owns the confirmation prompt; by the time this is called the create
    is approved (or ``--yes`` was given). Idempotent: ``already_initialized`` when
    ``cite.toml`` already exists.
    """
    if lib.is_initialized():
        return {"status": "already_initialized", "library": str(lib.root)}
    lib.init()
    return {"status": "created", "library": str(lib.root)}


def get(lib: Library, id: str) -> dict:
    """Return one reference's raw CSL-JSON record, or a ``not_found`` envelope.

    The success payload is the stored record itself (a raw CSL-JSON object, *not* an
    envelope) — the shell emits it without the spec stamp. The ``not_found`` branch
    is a normal envelope.
    """
    stem = local_id(id)
    try:
        return lib.read_record(stem)
    except FileNotFoundError:
        return {"status": "not_found", "id": namespaced_id(stem)}


def remove(lib: Library, id: str) -> dict:
    """Remove a reference bundle (record + file + extracted artifacts), all-or-nothing."""
    stem = local_id(id)
    try:
        lib.remove(stem)
        return {"status": "removed", "id": namespaced_id(stem), "deleted_file": True}
    except FileNotFoundError:
        return {"status": "not_found", "id": namespaced_id(stem)}


def update(
    lib: Library,
    id: str,
    *,
    fields: list[str] | None = None,
    remove_fields: list[str] | None = None,
    cite_type: str | None = None,
) -> dict:
    """Amend a stored reference's metadata in place — the missing CRUD verb.

    Applies the patches (``fields`` sets/replaces, ``remove_fields`` deletes,
    ``cite_type`` changes the type), re-validates, and rewrites the record. The
    document, its content hash, and provenance are preserved. When an edit touches
    an id-bearing field the whole bundle is renamed to match. Returns ``not_found``,
    ``error`` (no change / unknown type / id clash), ``missing_fields``, or
    ``updated``. Raises ``ValueError`` on a malformed field.
    """
    fields = fields or []
    remove_fields = remove_fields or []
    stem = local_id(id)
    try:
        record = lib.read_record(stem)
    except FileNotFoundError:
        return {"status": "not_found", "id": namespaced_id(stem)}

    if not fields and not remove_fields and not cite_type:
        return {
            "status": "error",
            "id": namespaced_id(stem),
            "message": "no changes given; pass --field, --remove-field, or --type",
        }

    prov = record.get(PROVENANCE_KEY, {})
    effective_type = cite_type or prov.get("cite_type")
    if effective_type not in CITE_TYPES:
        return {
            "status": "error",
            "id": namespaced_id(stem),
            "message": f"unknown cite_type {effective_type!r}; must be one of "
                       f"{', '.join(CITE_TYPES)}",
        }

    # A type change rewrites the CSL `type`/`genre` (and the stored cite_type).
    if cite_type:
        record["type"] = csl_type_for(cite_type)
        genre = genre_for(cite_type)
        if genre:
            record["genre"] = genre
        else:
            record.pop("genre", None)
        prov["cite_type"] = cite_type

    # Deletes first, then sets — so `--remove-field x --field x=...` ends up set.
    for key in remove_fields:
        record.pop(key.strip(), None)
    apply_fields(record, fields)

    # Re-validate the patched record; commit nothing if it's now incomplete.
    verdict = run_validate(effective_type, record)
    if verdict["status"] != "ok":
        verdict["id"] = namespaced_id(stem)
        return verdict

    # Recompute the id from the patched metadata + the (unchanged) content hash.
    full_hash = prov.get("file_hash")
    new_stem = record_id(record, full_hash) if full_hash else stem
    renamed = new_stem != stem
    if renamed:
        try:
            new_doc = lib.rename_bundle(stem, new_stem)
        except FileExistsError:
            return {
                "status": "error",
                "id": namespaced_id(stem),
                "message": f"target id {namespaced_id(new_stem)} already exists; "
                           "resolve the clash before updating",
            }
        if new_doc:
            prov["new_filename"] = new_doc
        extraction = prov.get("extraction")
        if isinstance(extraction, dict) and extraction.get("markdown_path"):
            extraction["markdown_path"] = f"{new_stem}/{new_stem}.md"

    record[PROVENANCE_KEY] = prov
    lib.write_record(new_stem, record)

    return {
        "status": "updated",
        "id": namespaced_id(new_stem),
        "renamed": renamed,
        "old_id": namespaced_id(stem) if renamed else None,
        "cite_type": effective_type,
        "new_filename": prov.get("new_filename"),
        "record": record,
    }


def extract(
    lib: Library, id: str, *, vlm_model: str = "granite_docling"
) -> dict:
    """Extract full markdown for a stored reference using a local Docling VLM.

    Writes ``<id>/<id>.md`` + artifacts into the bundle and records the extractor
    name/version under ``_provenance.extraction``. Returns ``not_found`` for an
    unknown id, ``error`` for a missing source file or a docling failure (including
    ``ExtractorUnavailable``). All non-``ok`` branches are returned dicts here; the
    shell decides exit codes.
    """
    from cite.extract import ExtractorUnavailable, extract_to_markdown

    stem = local_id(id)
    try:
        record = lib.read_record(stem)
    except FileNotFoundError:
        return {"status": "not_found", "id": namespaced_id(stem)}

    prov = record.get(PROVENANCE_KEY, {})
    new_filename = prov.get("new_filename")
    src = lib.entry_dir(stem) / new_filename if new_filename else None
    if src is None or not src.exists():
        return {
            "status": "error",
            "id": namespaced_id(stem),
            "message": "stored source file not found for this reference",
        }

    lib.clear_text(stem)  # idempotent re-extract: wipe any prior output first
    try:
        result = extract_to_markdown(src, lib.text_path(stem), vlm_model=vlm_model)
    except ExtractorUnavailable as e:
        return {"status": "error", "message": str(e), "hint": "install cite[extract]"}
    except Exception as e:  # docling runtime failure — surface, don't crash
        return {"status": "error", "message": f"extraction failed: {e}"}

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

    return {
        "status": "ok",
        "id": namespaced_id(stem),
        "markdown_path": md_rel,
        "n_images": result["n_images"],
        "extractor": result["extractor"],
        "extractor_version": result["extractor_version"],
    }


# --------------------------------------------------------------------------- #
# Read / view operations (list, text, export)
# --------------------------------------------------------------------------- #


def library_view(lib: Library, *, full: bool = False) -> list | dict:
    """List references. ``full`` returns the raw record list; else compact summaries.

    The compact form is the ``{"count", "references"}`` envelope (an envelope, so
    the shell stamps it); the full form is a bare list of records (no stamp).
    """
    records = lib.list_records()
    if full:
        return records
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
    return {"count": len(summaries), "references": summaries}


def text(lib: Library, id: str, *, path_only: bool = False) -> dict:
    """Locate a reference's extracted markdown.

    Returns a ``not_found`` envelope if it hasn't been extracted, else
    ``{"status": "ok", "path": ..., "content": ...}``. The shell renders the bare
    ``path`` or ``content`` string for a hit (markdown is not JSON), and emits the
    ``not_found`` envelope as JSON. ``path_only`` is a hint to the shell.
    """
    stem = local_id(id)
    if not lib.text_path(stem).exists():
        return {
            "status": "not_found",
            "id": namespaced_id(stem),
            "hint": "run `cite extract <id>` first (needs the 'extract' extra)",
        }
    return {
        "status": "ok",
        "path": str(lib.text_path(stem)),
        "content": lib.read_text(stem),
    }


# Minimal CSL -> BibTeX (leans on CSL being a standard).
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


def to_bibtex(record: dict) -> str:
    """Render one CSL-JSON record as a BibTeX entry."""
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


def export(lib: Library, fmt: str) -> str:
    """Serialise the whole library as ``csl``/``pandoc`` (CSL-JSON) or ``bibtex``.

    Returns the rendered string; raises ``ValueError`` for an unknown format. The
    output is a document (not an envelope) — the shell prints it verbatim.
    """
    records = lib.list_records()
    fmt = fmt.lower()
    if fmt in ("csl", "pandoc"):
        # Pandoc --citeproc consumes CSL-JSON directly.
        return json.dumps(records, indent=2, ensure_ascii=False)
    if fmt == "bibtex":
        return "\n\n".join(to_bibtex(r) for r in records)
    raise ValueError("format must be csl, bibtex, or pandoc")
