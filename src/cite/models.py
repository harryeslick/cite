"""The shared contract: the single source of truth for citation types.

Everything in `cite` binds to this module — the search backends, the validator,
the filename generator, the store, the CLI, and the agent-facing `cite guide`.
Keeping the type vocabulary, the CSL mapping, and the per-type required-field
table in one place is what lets the rest of the package stay deterministic and
internally consistent.

A "reference record" is a plain CSL-JSON object (a dict) — CSL-JSON is an open,
dynamic schema, so we model it as a dict rather than freezing it into a class.
The only structured part is the `_provenance` block we add for tracking, which
is a real pydantic model. CSL processors ignore the underscore-prefixed key, so
records stay both standard-compliant and self-describing.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Suite conformance (see SUITE.md)
# --------------------------------------------------------------------------- #

# Version of the shared response-envelope protocol this tool emits. Stamped onto
# every agent-facing response envelope so agents / sibling tools can detect the
# contract version. Bump only when the envelope, id scheme, or status vocabulary
# changes.
SPEC_VERSION = "suite/1"


# --------------------------------------------------------------------------- #
# Controlled vocabulary of citation types (the 7 the tool supports)
# --------------------------------------------------------------------------- #

CiteType = Literal[
    "journal-article",
    "book",
    "book-section",
    "web-site",
    "trial-report",
    "data-set",
    "other-report",
]

CITE_TYPES: tuple[str, ...] = get_args(CiteType)

# cite_type -> (CSL `type`, CSL `genre` or None)
# `genre` differentiates the two report flavours, which both map to CSL `report`.
TYPE_MAP: dict[str, tuple[str, str | None]] = {
    "journal-article": ("article-journal", None),
    "book": ("book", None),
    "book-section": ("chapter", None),
    "web-site": ("webpage", None),
    "trial-report": ("report", "trial report"),
    "data-set": ("dataset", None),
    "other-report": ("report", None),
}

# Reverse lookup for importing search results (CSL `type` -> our cite_type).
# `report` is ambiguous; default to `other-report` and let genre refine it.
_CSL_TO_CITE: dict[str, str] = {
    "article-journal": "journal-article",
    "article": "journal-article",
    "book": "book",
    "chapter": "book-section",
    "webpage": "web-site",
    "dataset": "data-set",
    "report": "other-report",
}


# --------------------------------------------------------------------------- #
# Per-type required fields (drives validation and the "ask the user" workflow)
# --------------------------------------------------------------------------- #

# A requirement is either a single CSL key (required), or a tuple of keys
# meaning "at least one of these must be present" (a one-of group).
Requirement = str | tuple[str, ...]

REQUIRED_FIELDS: dict[str, list[Requirement]] = {
    "journal-article": ["title", "author", "container-title", "issued"],
    "book": ["title", ("author", "editor"), "issued", "publisher"],
    "book-section": ["title", "author", "container-title", "issued"],
    "web-site": ["title", "URL", "accessed"],
    "trial-report": ["title", ("author", "publisher"), "issued"],
    "other-report": ["title", ("author", "publisher"), "issued"],
    "data-set": ["title", ("DOI", "URL"), "issued"],
}


# --------------------------------------------------------------------------- #
# Provenance: our custom tracking block, stored under the `_provenance` key
# --------------------------------------------------------------------------- #


class Extraction(BaseModel):
    """Record of a full-markdown extraction (see `cite extract`).

    Stored under `_provenance.extraction`. Docling is ML-based, so output is
    reproducible only relative to a pinned `extractor_version` — we record it so
    a stale extraction (file changed, or extractor upgraded) is detectable later.
    """

    extractor: str  # e.g. "docling"
    extractor_version: str
    vlm_model: str  # e.g. "granite_docling"
    image_export_mode: str  # e.g. "referenced"
    extracted_at: str  # ISO-8601 UTC
    source_file_hash: str  # full SHA-256 of the bytes the markdown came from
    markdown_path: str  # "<id>/<id>.md", relative to the library root
    n_images: int


class Provenance(BaseModel):
    """Custom metadata tracked alongside the standard CSL fields."""

    cite_type: CiteType
    original_filename: str
    new_filename: str
    date_added: str  # ISO-8601 UTC, e.g. "2026-06-04T16:10:00Z"
    file_hash: str  # full SHA-256 hex of the file's bytes
    source: Literal["crossref", "datacite", "openalex", "manual", "web"]
    source_id: str | None = None  # DOI / OpenAlex ID, when applicable
    extraction: Extraction | None = None  # set by `cite extract`


PROVENANCE_KEY = "_provenance"


# --------------------------------------------------------------------------- #
# Field-presence helpers (the basis of deterministic validation)
# --------------------------------------------------------------------------- #


def has_field(record: dict[str, Any], key: str) -> bool:
    """True if `key` is present in the CSL record with a meaningful value.

    `issued` is special: it must contain an actual year, not just an empty
    date-parts shell. Lists (author/editor) must be non-empty. Everything else
    must simply be truthy.
    """
    value = record.get(key)
    if value is None:
        return False
    if key == "issued":
        return _issued_year(value) is not None
    if isinstance(value, (list, str, dict)):
        return len(value) > 0
    return bool(value)


def _issued_year(issued: Any) -> int | None:
    """Extract the 4-digit year from a CSL `issued` date object, if present."""
    if not isinstance(issued, dict):
        return None
    parts = issued.get("date-parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
        try:
            return int(parts[0][0])
        except (TypeError, ValueError):
            return None
    return None


def issued_year(record: dict[str, Any]) -> int | None:
    """Public helper: the publication year of a record, or None."""
    return _issued_year(record.get("issued"))


# --------------------------------------------------------------------------- #
# Validation + mapping API (consumed by validate.py, search/*, naming.py, cli)
# --------------------------------------------------------------------------- #


def csl_type_for(cite_type: str) -> str:
    """Map our cite_type to the CSL `type` value."""
    _ensure_known(cite_type)
    return TYPE_MAP[cite_type][0]


def genre_for(cite_type: str) -> str | None:
    """Map our cite_type to a CSL `genre`, if one applies."""
    _ensure_known(cite_type)
    return TYPE_MAP[cite_type][1]


def cite_type_from_csl(csl_type: str | None, genre: str | None = None) -> str:
    """Best-effort reverse map for imported search results."""
    base = _CSL_TO_CITE.get((csl_type or "").lower(), "other-report")
    if base == "other-report" and genre and "trial" in genre.lower():
        return "trial-report"
    return base


def missing_required_fields(cite_type: str, record: dict[str, Any]) -> list[str]:
    """Return the required fields a record is missing for its type.

    One-of groups are reported as a single "a|b" token so the agent knows that
    supplying either satisfies the requirement. An empty list means valid.
    """
    _ensure_known(cite_type)
    missing: list[str] = []
    for req in REQUIRED_FIELDS[cite_type]:
        if isinstance(req, tuple):
            if not any(has_field(record, k) for k in req):
                missing.append("|".join(req))
        elif not has_field(record, req):
            missing.append(req)
    return missing


def _ensure_known(cite_type: str) -> None:
    if cite_type not in TYPE_MAP:
        raise ValueError(
            f"Unknown cite_type {cite_type!r}; must be one of {', '.join(CITE_TYPES)}"
        )


def provenance_field() -> Any:
    """Convenience for building a record dict with the provenance under its key."""
    return Field(alias=PROVENANCE_KEY)
