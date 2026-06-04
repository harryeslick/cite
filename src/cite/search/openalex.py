"""OpenAlex title/author/year search backend."""

from __future__ import annotations

import httpx

_OPENALEX_TYPE_MAP: dict[str, str] = {
    "article": "article-journal",
    "book-chapter": "chapter",
}


def search(
    title: str,
    author: str | None = None,
    year: int | None = None,
    rows: int = 5,
) -> list[dict]:
    """Search OpenAlex for works matching title (and optionally author/year).

    Returns a list of CSL-JSON dicts (may be empty on error).
    Each dict includes an ``openalex_id`` key for the orchestrator to use
    as source_id.
    """
    params: dict = {
        "search": title,
        "per-page": rows,
        "mailto": "32809214+harryeslick@users.noreply.github.com",
    }
    if year is not None:
        params["filter"] = f"publication_year:{year}"

    try:
        response = httpx.get(
            "https://api.openalex.org/works",
            params=params,
            timeout=15.0,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return []

    if not response.is_success:
        return []

    try:
        data = response.json()
    except Exception:
        return []

    results = data.get("results") or []
    candidates = [_parse_result(r) for r in results]

    # Optional author filtering/boosting: put author-matching results first
    if author and candidates:
        author_lower = author.lower()

        def _author_match(c: dict) -> bool:
            for a in c.get("author") or []:
                name = (
                    " ".join(filter(None, [a.get("given"), a.get("family")]))
                    or a.get("literal", "")
                )
                if author_lower in name.lower():
                    return True
            return False

        matched = [c for c in candidates if _author_match(c)]
        unmatched = [c for c in candidates if not _author_match(c)]
        candidates = matched + unmatched

    return candidates


def _parse_result(result: dict) -> dict:
    """Convert a single OpenAlex work result to a CSL-JSON dict."""
    record: dict = {}

    # title
    display_name = result.get("display_name") or ""
    if display_name:
        record["title"] = display_name

    # author: from authorships
    authorships = result.get("authorships") or []
    if authorships:
        authors = []
        for a in authorships:
            name = (a.get("author") or {}).get("display_name") or ""
            if name:
                tokens = name.split()
                if len(tokens) >= 2:
                    authors.append({
                        "family": tokens[-1],
                        "given": " ".join(tokens[:-1]),
                    })
                else:
                    authors.append({"literal": name})
        if authors:
            record["author"] = authors

    # issued
    pub_year = result.get("publication_year")
    if pub_year is not None:
        try:
            record["issued"] = {"date-parts": [[int(pub_year)]]}
        except (TypeError, ValueError):
            pass

    # DOI: strip https://doi.org/ prefix
    doi = result.get("doi") or ""
    if doi:
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]
        elif doi.startswith("http://doi.org/"):
            doi = doi[len("http://doi.org/"):]
        record["DOI"] = doi

    # container-title from primary_location.source
    primary_location = result.get("primary_location") or {}
    source = primary_location.get("source") or {}
    source_name = source.get("display_name") or ""
    if source_name:
        record["container-title"] = source_name

    # type mapping
    raw_type = result.get("type") or ""
    record["type"] = _OPENALEX_TYPE_MAP.get(raw_type, raw_type)

    # openalex_id for orchestrator to use as source_id
    oa_id = result.get("id") or ""
    record["openalex_id"] = oa_id

    return record
