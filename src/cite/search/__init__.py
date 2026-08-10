"""Public search orchestrator for the cite package."""

from __future__ import annotations

import re

from cite.models import cite_type_from_csl
from cite.search import crossref, datacite, net, openalex
from cite.search.net import SearchUnavailable

# Title-relevance gate for the OpenAlex (title) path. OpenAlex always returns its
# top-N by text relevance, so an unmatched query (e.g. grey literature with no DB
# record) still comes back full of unrelated papers. We drop any candidate whose
# title shares fewer than this fraction of the query's content words — a
# deterministic filter that keeps real matches and discards the noise, instead of
# forcing the agent to read and reject every hit.
_TITLE_OVERLAP_MIN = 0.34
# Cap on how many candidates we return for the agent to choose between.
_MAX_CANDIDATES = 3
# Words ignored when comparing titles (too common to carry signal).
_STOPWORDS = frozenset(
    "the a an of and or for on to in by with from at as is are be this that "
    "using use update report guide".split()
)


def search(
    doi: str | None = None,
    title: str | None = None,
    author: str | None = None,
    year: int | None = None,
) -> dict:
    """Search for a reference record by DOI or by title/author/year.

    Returns a dict with keys:
      - ``status``: "ok", "not_found", "weak_match" or "unavailable"
      - ``query``: echo of the input parameters
      - ``candidates``: list of CSL-JSON dicts (may be empty)
      - ``suggested_next``: a hint string for the next step
      - ``unavailable_sources``: present only when a backend could not be
        reached — one compact entry per source (see net.SearchUnavailable)

    ``not_found`` means the sources answered and had nothing. ``unavailable``
    means we never got an answer, so the caller must retry rather than give up
    and hand-enter metadata. The distinction is the whole point of the
    ``unavailable_sources`` key: a search can succeed on one source while
    another is rate-limited, and silently returning the shorter candidate list
    would be the same bug at a smaller scale.

    Each candidate carries two extra keys beyond standard CSL:
      - ``source``: "crossref" | "datacite" | "openalex"
      - ``source_id``: the DOI or OpenAlex id
      - ``_cite_type``: our controlled-vocabulary type (from cite_type_from_csl)
    """
    query = {
        "doi": doi,
        "title": title,
        "author": author,
        "year": year,
    }
    candidates: list[dict] = []
    unavailable: list[SearchUnavailable] = []

    if doi is not None:
        # DOI path: try CrossRef first, then DataCite. Each is attempted on its
        # own so one being down doesn't hide what the other knows.
        record = None
        try:
            record = crossref.fetch_doi(doi)
        except SearchUnavailable as exc:
            unavailable.append(exc)
        if record is not None:
            _annotate(record, source="crossref", source_id=doi)
            candidates.append(record)
        else:
            try:
                record = datacite.fetch_doi(doi)
            except SearchUnavailable as exc:
                unavailable.append(exc)
            if record is not None:
                _annotate(record, source="datacite", source_id=doi)
                candidates.append(record)

    elif title is not None:
        # Title path: OpenAlex. Filter out low-relevance noise so the agent isn't
        # forced to read and reject unrelated papers (see _TITLE_OVERLAP_MIN).
        try:
            results = openalex.search(title, author=author, year=year)
        except SearchUnavailable as exc:
            return _unavailable_envelope(query, [exc])
        relevant = [r for r in results if _title_overlap(title, r) >= _TITLE_OVERLAP_MIN]
        for r in relevant:
            oa_id = r.pop("openalex_id", "") or ""
            source_id = r.get("DOI") or oa_id or None
            _annotate(r, source="openalex", source_id=source_id)
        # Return compact pick-list summaries, not full CSL: the agent only needs
        # enough to choose. It then files the choice by source_id via add_by_doi,
        # which re-fetches the complete, authoritative record (so trimming the
        # author list here never truncates the stored bibliography).
        candidates = [_summarize(r) for r in relevant[:_MAX_CANDIDATES]]
        # Distinguish "DB had nothing" from "DB had only unrelated hits" — the
        # latter still means go-manual, but signals the search wasn't wasted.
        if not candidates and results:
            return {
                "status": "weak_match",
                "query": query,
                "candidates": [],
                "note": (
                    f"OpenAlex returned {len(results)} result(s) but none matched "
                    "the title closely; this is likely grey literature or an "
                    "untitled/non-indexed work."
                ),
                "suggested_next": _MANUAL_HINT,
            }

    # Nothing found *and* a source we never heard back from: we cannot claim the
    # work is absent, so this is a retry, not a go-manual.
    if not candidates and unavailable:
        return _unavailable_envelope(query, unavailable)

    status = "ok" if candidates else "not_found"

    if not candidates:
        suggested_next = _MANUAL_HINT
    elif doi is not None:
        # DOI path returns one exact, full CSL record — file it directly.
        suggested_next = "cite add <file> --csl - (pipe the chosen candidate)"
    else:
        # Title path returns summaries — file the chosen one by its DOI.
        suggested_next = (
            "file the chosen candidate by its DOI: cite add <file> --doi "
            "<source_id>. If a candidate has no DOI, fall back to --manual."
        )

    envelope = {
        "status": status,
        "query": query,
        "candidates": candidates,
        "suggested_next": suggested_next,
    }
    # Partial failure: we did find something, but not everywhere we looked.
    if unavailable:
        envelope["unavailable_sources"] = [exc.as_dict() for exc in unavailable]
    return envelope


_MANUAL_HINT = (
    "no match found; gather fields and use: "
    "cite add <file> --manual --type <type> --field key=val ..."
)


def _unavailable_envelope(query: dict, failures: list[SearchUnavailable]) -> dict:
    """Envelope for "we couldn't ask" — never to be confused with "not there".

    The hint is emphatic on purpose: the failure mode this replaces was an agent
    reading "no match found" after a 429 and hand-typing a record that OpenAlex
    would have returned a minute later.
    """
    waits = [f.retry_after for f in failures if f.retry_after is not None]
    if waits:
        wait_hint = f"wait {max(waits)}s, then retry the identical search"
    else:
        wait_hint = "wait ~30s, then retry the identical search"
    sources = ", ".join(sorted({f.source for f in failures}))
    hint = (
        f"could not reach {sources} — this is NOT a 'no match'. "
        f"{wait_hint}. Do not enter metadata by hand on the strength of this result"
    )
    if any(f.kind == net.RATE_LIMITED and f.source == "openalex" for f in failures):
        hint += (
            f"; a free OpenAlex key in ${net.OPENALEX_API_KEY_ENV} raises the "
            "daily budget 10x"
        )
    return {
        "status": "unavailable",
        "query": query,
        "candidates": [],
        "unavailable_sources": [f.as_dict() for f in failures],
        "suggested_next": hint,
    }


def _tokens(text: str) -> set[str]:
    """Lowercase content words (len >= 3) of a title, minus stopwords."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _title_overlap(query_title: str, candidate: dict) -> float:
    """Fraction of the query's content words present in the candidate title.

    1.0 means every query word appears in the candidate; 0.0 means none do.
    Containment (over the query, not a symmetric Jaccard) so a long candidate
    title that contains the short query still scores high.
    """
    q = _tokens(query_title)
    if not q:
        return 1.0  # nothing to discriminate on — don't filter
    c = _tokens(candidate.get("title", ""))
    return len(q & c) / len(q)


def _summarize(record: dict) -> dict:
    """Compact a full CSL candidate to a pick-list entry.

    Uses deliberately non-CSL keys (``authors`` string, ``year`` int) so the
    summary can't be mistaken for a complete record and piped into add_from_csl;
    the agent files it by ``source_id`` instead.
    """
    summary: dict = {"title": record.get("title")}
    authors = _author_display(record.get("author") or [])
    if authors:
        summary["authors"] = authors
    year = _issued_year(record)
    if year is not None:
        summary["year"] = year
    for key in ("container-title", "DOI", "source", "source_id", "_cite_type"):
        value = record.get(key)
        if value:
            summary[key] = value
    return summary


def _author_display(authors: list[dict]) -> str | None:
    """First author + 'et al. (N)' for N>1 — a one-line author summary."""
    if not authors:
        return None
    first = authors[0]
    family = first.get("family") or first.get("literal") or ""
    given = first.get("given")
    label = f"{family}, {given}" if (family and given) else family
    n = len(authors)
    return f"{label} et al. ({n})" if n > 1 else label


def _issued_year(record: dict) -> int | None:
    parts = (record.get("issued") or {}).get("date-parts") or []
    if parts and parts[0]:
        return parts[0][0]
    return None


def _annotate(record: dict, source: str, source_id: str | None) -> None:
    """Add source, source_id, and _cite_type keys to a candidate record in-place."""
    record["source"] = source
    record["source_id"] = source_id
    csl_type = record.get("type")
    genre = record.get("genre")
    record["_cite_type"] = cite_type_from_csl(csl_type, genre)
