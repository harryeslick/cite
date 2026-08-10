"""DataCite DOI lookup backend."""

from __future__ import annotations

from cite.search import net


def fetch_doi(doi: str) -> dict | None:
    """Fetch a record from DataCite by DOI.

    Returns a CSL-JSON dict on success and None on a genuine 404. Failures that
    prevent an answer raise ``net.SearchUnavailable`` (see crossref.fetch_doi).
    Does NOT add source/source_id — the orchestrator does that.
    """
    response = net.get(
        f"https://api.datacite.org/dois/{doi}",
        source="datacite",
        headers={"Accept": "application/vnd.api+json"},
    )
    if response.status_code == 404:
        return None

    data = net.parse_json(response, "datacite")
    attrs = (data.get("data") or {}).get("attributes") or {}
    return _parse_attributes(attrs)


def _parse_attributes(attrs: dict) -> dict:
    """Convert DataCite attributes dict into a CSL-JSON dict."""
    record: dict = {}

    # title: first element of titles list
    titles = attrs.get("titles") or []
    if titles:
        record["title"] = titles[0].get("title", "")

    # author: from creators list
    creators = attrs.get("creators") or []
    if creators:
        authors = []
        for c in creators:
            family = c.get("familyName")
            given = c.get("givenName")
            name = c.get("name", "")
            if family:
                entry: dict = {"family": family}
                if given:
                    entry["given"] = given
                authors.append(entry)
            elif name:
                authors.append({"literal": name})
        if authors:
            record["author"] = authors

    # issued: publicationYear as integer
    pub_year = attrs.get("publicationYear")
    if pub_year is not None:
        try:
            record["issued"] = {"date-parts": [[int(pub_year)]]}
        except (TypeError, ValueError):
            pass

    # publisher
    publisher = attrs.get("publisher")
    if publisher:
        record["publisher"] = publisher

    # DOI
    doi = attrs.get("doi")
    if doi:
        record["DOI"] = doi

    # URL
    url = attrs.get("url")
    if url:
        record["URL"] = url

    # type: dataset vs report
    resource_type_general = (
        (attrs.get("types") or {}).get("resourceTypeGeneral") or ""
    )
    if resource_type_general == "Dataset":
        record["type"] = "dataset"
    else:
        record["type"] = "report"

    return record
