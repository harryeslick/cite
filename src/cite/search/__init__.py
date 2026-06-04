"""Public search orchestrator for the cite package."""

from __future__ import annotations

from cite.models import cite_type_from_csl
from cite.search import crossref, datacite, openalex


def search(
    doi: str | None = None,
    title: str | None = None,
    author: str | None = None,
    year: int | None = None,
) -> dict:
    """Search for a reference record by DOI or by title/author/year.

    Returns a dict with keys:
      - ``status``: "ok" or "empty"
      - ``query``: echo of the input parameters
      - ``candidates``: list of CSL-JSON dicts (may be empty)
      - ``suggested_next``: a hint string for the next step

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

    if doi is not None:
        # DOI path: try CrossRef first, then DataCite
        record = crossref.fetch_doi(doi)
        if record is not None:
            _annotate(record, source="crossref", source_id=doi)
            candidates.append(record)
        else:
            record = datacite.fetch_doi(doi)
            if record is not None:
                _annotate(record, source="datacite", source_id=doi)
                candidates.append(record)

    elif title is not None:
        # Title path: OpenAlex
        results = openalex.search(title, author=author, year=year)
        for r in results:
            oa_id = r.pop("openalex_id", "") or ""
            source_id = r.get("DOI") or oa_id or None
            _annotate(r, source="openalex", source_id=source_id)
        candidates = results

    status = "ok" if candidates else "empty"

    if candidates:
        suggested_next = (
            "cite add <file> --csl - (pipe the chosen candidate)"
        )
    else:
        suggested_next = (
            "no match found; gather fields and use: "
            "cite add <file> --manual --type <type> --field key=val ..."
        )

    return {
        "status": status,
        "query": query,
        "candidates": candidates,
        "suggested_next": suggested_next,
    }


def _annotate(record: dict, source: str, source_id: str | None) -> None:
    """Add source, source_id, and _cite_type keys to a candidate record in-place."""
    record["source"] = source
    record["source_id"] = source_id
    csl_type = record.get("type")
    genre = record.get("genre")
    record["_cite_type"] = cite_type_from_csl(csl_type, genre)
