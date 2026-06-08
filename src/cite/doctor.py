"""Library health check — the engine behind ``cite doctor``.

A pure, read-only integrity sweep of a whole library. It exists so the library
stays trustworthy over time: records drift, files get moved by hand, an edit can
leave a bundle half-renamed. ``run_doctor`` walks every bundle and reports the
problems it finds, emitting nothing to disk and touching no network.

Why walk the filesystem directly instead of ``Library.list_records()``? Because
``list_records`` silently *skips* any bundle whose ``<id>/<id>.json`` won't parse
(see store.py) — which is exactly the corruption a health check must surface. So
this module re-walks the root itself, applying the same skip rules for non-bundle
entries (``cite.toml``, the ``.staging/`` cache) that ``list_records`` uses.

The report is context-frugal (SUITE.md §3): a summary plus one short line per
problem, never the full records.
"""

from __future__ import annotations

import json

from cite.models import PROVENANCE_KEY, missing_required_fields
from cite.naming import namespaced_id
from cite.store import Library

# Problem-class identifiers, also used as the keys of the summary histogram.
INVALID_JSON = "invalid_json"
MISSING_PROVENANCE = "missing_provenance"
MISSING_FIELDS = "missing_fields"
MISSING_FILE = "missing_file"
ORPHAN = "orphan"


def _problem(record_id: str, problem: str, detail: str) -> dict:
    """One compact problem entry. Keep ``detail`` to a single short line."""
    return {"id": namespaced_id(record_id), "problem": problem, "detail": detail}


def _check_bundle(lib: Library, record_id: str) -> list[dict]:
    """Inspect one bundle directory; return its problems (empty list if healthy)."""
    record_path = lib.record_path(record_id)

    # No record file at all → the directory is an orphan bundle.
    if not record_path.exists():
        return [_problem(record_id, ORPHAN, "bundle directory has no record JSON")]

    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [_problem(record_id, INVALID_JSON, f"cannot parse record: {exc}")]

    problems: list[dict] = []
    provenance = record.get(PROVENANCE_KEY)
    cite_type = provenance.get("cite_type") if isinstance(provenance, dict) else None

    # Provenance is required to know the type (and the stored filename); without
    # it we can't run the field check, so report and stop here for this bundle.
    if not cite_type:
        problems.append(
            _problem(record_id, MISSING_PROVENANCE, "no _provenance.cite_type")
        )
        return problems

    # Required-field check for the record's declared type.
    try:
        missing = missing_required_fields(cite_type, record)
    except ValueError:
        problems.append(
            _problem(record_id, MISSING_PROVENANCE, f"unknown cite_type {cite_type!r}")
        )
        missing = []
    if missing:
        problems.append(
            _problem(record_id, MISSING_FIELDS, f"missing: {', '.join(missing)}")
        )

    # The stored source document must still be present in the bundle.
    new_filename = provenance.get("new_filename")
    if new_filename and not (lib.entry_dir(record_id) / new_filename).exists():
        problems.append(
            _problem(record_id, MISSING_FILE, f"source file absent: {new_filename}")
        )

    return problems


def run_doctor(lib: Library) -> dict:
    """Validate the whole library and return a context-frugal health envelope.

    Walks every bundle directory under the library root (skipping ``cite.toml``
    and the dotted ``.staging/`` cache, mirroring ``Library.list_records``) and
    aggregates problems by class. ``status`` is ``"ok"`` only when nothing is
    wrong, else ``"problems"``.
    """
    problems: list[dict] = []
    checked = 0

    if lib.root.is_dir():
        for entry in sorted(lib.root.iterdir(), key=lambda p: p.name):
            # Skip non-bundle entries: files (cite.toml) and dotted dirs (.staging).
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            checked += 1
            problems.extend(_check_bundle(lib, entry.name))

    summary: dict[str, int] = {}
    for p in problems:
        summary[p["problem"]] = summary.get(p["problem"], 0) + 1

    return {
        "status": "ok" if not problems else "problems",
        "checked": checked,
        "healthy": checked - len({p["id"] for p in problems}),
        "problem_count": len(problems),
        "problems": problems,
        "summary": summary,
    }
