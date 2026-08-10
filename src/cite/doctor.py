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
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util

from cite import __version__
from cite.models import PROVENANCE_KEY, missing_required_fields
from cite.naming import namespaced_id
from cite.search import net
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

    # Same check for each attached supplement: the record claims the file is in
    # the bundle, so a claim it can't back is exactly the MISSING_FILE class.
    for supplement in provenance.get("supplements") or []:
        filename = supplement.get("filename")
        if filename and not (lib.entry_dir(record_id) / filename).exists():
            problems.append(
                _problem(
                    record_id, MISSING_FILE, f"supplement file absent: {filename}"
                )
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


# --------------------------------------------------------------------------- #
# Self-check — the install and its environment, not the library's contents
# --------------------------------------------------------------------------- #

# Maps each optional extra (declared in pyproject's [project.optional-dependencies])
# to the import name that proves it is actually installed. We probe the *module*
# rather than `importlib.metadata` because what callers care about is "can the
# feature run", and probing keeps it cheap: `find_spec` only locates the module on
# sys.path, it never imports it — so checking `extract` does not drag in docling/torch.
_EXTRA_PROBES = {"mcp": "mcp", "extract": "docling"}


def installed_extras() -> dict[str, bool]:
    """Report which optional extras are available, e.g. {'mcp': True, 'extract': False}."""
    return {
        extra: importlib_util.find_spec(module) is not None
        for extra, module in _EXTRA_PROBES.items()
    }


def run_self_check(lib: Library) -> dict:
    """Report on the *install* rather than the library's contents.

    Answers the questions that silently derail a session: is the `cite-mcp`
    entrypoint able to start (it is registered by the host, so a missing `mcp`
    extra shows up only as a server that never appears); is the extract engine
    available; and which library root did we land on and how. Read-only, no
    network, no heavy imports.
    """
    extras = installed_extras()

    try:
        version = importlib_metadata.version("cite")
    except importlib_metadata.PackageNotFoundError:
        version = __version__

    # The MCP server is a separate entrypoint launched by the *host*, so its
    # failure is invisible here unless we say so explicitly.
    mcp_status = (
        "ok"
        if extras["mcp"]
        else "broken: the 'mcp' extra is not installed, so `cite-mcp` exits at startup"
    )

    # Neither identity setting is required — both only widen the rate/credit
    # budget the search backends get — so an absent one is reported, not a problem.
    identity = net.identity_report()

    hints = []
    missing = [name for name, present in extras.items() if not present]
    if missing:
        hints.append(f"install missing extras: uv tool install --force 'cite[all]'")
    if not lib.is_initialized():
        hints.append(
            f"no library at '{lib.root}' — pass --library <path> or run `cite init`"
        )
    if not identity["openalex_api_key"]:
        hints.append(
            "searches run on OpenAlex's $0.10/day unauthenticated budget; a free key "
            f"raises it to $1/day (openalex.org/settings/api) — set "
            f"{net.OPENALEX_API_KEY_KEY} under [{net.CONFIG_SECTION}] in "
            f"{lib.root / 'cite.toml'}, or ${net.OPENALEX_API_KEY_ENV}"
        )

    return {
        "status": "ok" if not missing and lib.is_initialized() else "problems",
        "cite_version": version,
        "extras": extras,
        "mcp_entrypoint": mcp_status,
        "library": {
            "resolved": str(lib.root),
            "origin": lib.origin,
            "initialized": lib.is_initialized(),
            "searched_from": str(lib.searched_from) if lib.searched_from else None,
        },
        "search_identity": identity,
        "extract_engines": {
            # Both engines ship with docling; neither is usable without it.
            "text": extras["extract"],
            "vlm": extras["extract"],
        },
        "hints": hints,
    }
