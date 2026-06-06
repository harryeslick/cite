"""Near-duplicate detection: a metadata-level gate layered on the exact-hash gate.

The store's ``find_by_hash`` only catches *identical bytes*. This module catches the
case the hash gate is blind to: a record that is the **same intellectual work** as one
already filed — a different scan of the same paper, a preprint vs the published
version, a re-download with different bytes — by comparing the *resolved CSL metadata*
(DOI, title, first author, year) of an incoming record against the existing library.

It is pure logic with no I/O (the role ``naming.py`` plays for hashing/filenames),
so the store stays a pure I/O layer. The CLI feeds it ``lib.list_records()``.

Determinism: every match decision is reproducible. The ``definitive`` and ``strong``
tiers are exact normalized equalities. The ``possible`` tier uses a fixed stdlib
``difflib`` similarity with a fixed threshold — heuristic, but reproducible — and only
ever drives a *question* to the user, never a silent commit.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit

from cite.models import issued_year
from cite.naming import namespaced_id

# Fixed similarity threshold for the fuzzy `possible` tier (token-set ratio in [0, 1]).
# High enough to keep false positives down; the user only sees `possible` as a prompt.
FUZZY_TITLE_THRESHOLD = 0.85

# Minimal stopword set dropped from the title token set so word order and filler
# ("the", "of", ...) don't sway the similarity score.
_STOPWORDS = frozenset(
    {"a", "an", "the", "of", "and", "or", "for", "to", "in", "on", "with", "by"}
)

_DOI_PREFIX = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------- #
# Normalization helpers (pure; each returns None/"" when the signal is absent)
# --------------------------------------------------------------------------- #


def normalize_doi(doi: str | None) -> str | None:
    """Lowercase a DOI and strip any ``doi.org`` URL prefix. None if absent/blank."""
    if not doi or not isinstance(doi, str):
        return None
    cleaned = _DOI_PREFIX.sub("", doi.strip()).lower()
    return cleaned or None


def normalize_url(url: str | None) -> str | None:
    """Canonicalize a URL for *exact* equality comparison. None if absent/blank.

    Deliberately coarse — the goal is "is this the same page", not "is this a
    valid URL". Lowercases scheme + host, drops the fragment AND the query string
    (so tracking/session noise like ``?utm_source=…`` doesn't make the same page
    look new), and trims a single trailing slash from the path. The documented
    consequence of dropping the query is that pages distinguished *only* by a
    query arg (``…?id=1`` vs ``…?id=2``) normalize equal — an accepted trade-off.
    """
    if not url or not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    # A bare "example.com/x" (no scheme) parses with an empty netloc; keep it
    # comparable by falling back to the raw path in that case.
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    cleaned = urlunsplit((scheme, netloc, path, "", ""))
    return cleaned or None


def normalize_title(title: str | None) -> str:
    """Lowercase, strip punctuation, collapse whitespace. "" when absent."""
    if not title or not isinstance(title, str):
        return ""
    return _NON_ALNUM.sub(" ", title.lower()).strip()


def _title_token_key(title: str | None) -> str:
    """Order-independent token-set key: normalized, stopworded, sorted, rejoined."""
    norm = normalize_title(title)
    if not norm:
        return ""
    tokens = sorted(t for t in norm.split() if t and t not in _STOPWORDS)
    return " ".join(tokens)


def title_similarity(a: str | None, b: str | None) -> float:
    """Token-set similarity of two titles in [0, 1] (0 if either is empty)."""
    key_a, key_b = _title_token_key(a), _title_token_key(b)
    if not key_a or not key_b:
        return 0.0
    return SequenceMatcher(None, key_a, key_b).ratio()


def first_author_family(record: dict) -> str | None:
    """Normalized family/literal of the first author (fallback: first editor)."""
    authors = record.get("author") or record.get("editor") or []
    if not isinstance(authors, list) or not authors:
        return None
    first = authors[0]
    if not isinstance(first, dict):
        return None
    raw = first.get("family") or first.get("literal") or ""
    norm = _NON_ALNUM.sub(" ", str(raw).lower()).strip()
    return norm or None


def _record_ref(record: dict) -> str | None:
    """The namespaced id of a stored record, recovered from its provenance filename.

    Mirrors the existing ``duplicate`` branch in ``cli.add`` (``cite.py``): a stored
    record's id is its filename stem. Returns None for records without provenance
    (e.g. an in-memory incoming record), which is fine — only candidates need ids.
    """
    prov = record.get("_provenance")
    new_filename = prov.get("new_filename", "") if isinstance(prov, dict) else ""
    stem = new_filename.rsplit(".", 1)[0] if new_filename else ""
    return namespaced_id(stem) if stem else None


def _summary(record: dict) -> dict:
    """A compact human-facing fingerprint of a candidate for the response envelope."""
    return {
        "title": record.get("title"),
        "author": first_author_family(record),
        "year": issued_year(record),
        "DOI": normalize_doi(record.get("DOI")),
        "URL": record.get("URL"),
    }


# --------------------------------------------------------------------------- #
# Match result
# --------------------------------------------------------------------------- #


@dataclass
class Match:
    """One existing record judged to be a possible/strong/definitive duplicate.

    ``tier`` drives the agent's policy: ``definitive``/``strong`` are deterministic
    and the agent may act on them without asking; ``possible`` is the heuristic,
    ambiguous bucket the agent should surface to the user before forcing the add.
    """

    id: str | None
    tier: str  # "definitive" | "strong" | "possible"
    score: float
    matched_on: list[str]
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# Sort key: definitive first, then strong, then possible; ties broken by score desc.
_TIER_RANK = {"definitive": 0, "strong": 1, "possible": 2}


def _classify(incoming: dict, existing: dict) -> Match | None:
    """Judge one existing record against the incoming one. None if not a candidate."""
    ref = _record_ref(existing)

    # Tier 1 — definitive: same registered object (exact normalized DOI).
    in_doi = normalize_doi(incoming.get("DOI"))
    ex_doi = normalize_doi(existing.get("DOI"))
    if in_doi and ex_doi and in_doi == ex_doi:
        return Match(ref, "definitive", 1.0, ["doi"], _summary(existing))

    # Everything below is title-based; no usable title on either side -> no signal.
    in_title_key = _title_token_key(incoming.get("title"))
    ex_title_key = _title_token_key(existing.get("title"))
    if not in_title_key or not ex_title_key:
        return None

    title_exact = in_title_key == ex_title_key
    sim = 1.0 if title_exact else title_similarity(incoming.get("title"), existing.get("title"))

    in_fam, ex_fam = first_author_family(incoming), first_author_family(existing)
    in_year, ex_year = issued_year(incoming), issued_year(existing)
    author_match = bool(in_fam and ex_fam and in_fam == ex_fam)
    year_match = bool(in_year and ex_year and in_year == ex_year)

    # Tier 2 — strong: exact title + same first author + same year (deterministic).
    if title_exact and author_match and year_match:
        return Match(ref, "strong", 1.0, ["title", "author", "year"], _summary(existing))

    # Tier 3 — possible: fuzzy title at/above threshold (author/year as corroboration).
    if sim >= FUZZY_TITLE_THRESHOLD:
        matched_on = ["title" if title_exact else f"title~{sim:.2f}"]
        if author_match:
            matched_on.append("author")
        if year_match:
            matched_on.append("year")
        return Match(ref, "possible", round(sim, 2), matched_on, _summary(existing))

    return None


def find_near_duplicates(incoming: dict, existing: list[dict]) -> list[Match]:
    """Return existing records that look like the same work as ``incoming``.

    Pure: scans ``existing`` (typically ``lib.list_records()``), classifies each into
    a confidence tier, and returns the matches ordered definitive -> strong -> possible
    (ties by descending score). Empty list means the file is clear to add.
    """
    matches = [m for rec in existing if (m := _classify(incoming, rec))]
    matches.sort(key=lambda m: (_TIER_RANK.get(m.tier, 9), -m.score))
    return matches


def find_url_duplicate(incoming: dict, existing: list[dict]) -> list[Match]:
    """Return existing records that share the incoming record's URL exactly.

    The dedup gate for the *web* path. Unlike :func:`find_near_duplicates`, this
    compares the **URL alone** (normalized by :func:`normalize_url`): an exact match
    is the only thing rejected — different URLs are always treated as new sources,
    regardless of title/author overlap. Returns ``definitive`` matches (deterministic
    equality), or an empty list when the page is clear to add. No-op when the
    incoming record carries no URL.
    """
    in_url = normalize_url(incoming.get("URL"))
    if not in_url:
        return []
    matches = []
    for rec in existing:
        if normalize_url(rec.get("URL")) == in_url:
            matches.append(
                Match(_record_ref(rec), "definitive", 1.0, ["url"], _summary(rec))
            )
    return matches
