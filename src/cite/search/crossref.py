"""CrossRef DOI lookup backend."""

from __future__ import annotations

from cite.search import net

_CROSSREF_TYPE_MAP: dict[str, str] = {
    "journal-article": "article-journal",
    "book-chapter": "chapter",
    "book": "book",
    "dataset": "dataset",
    "report": "report",
    "posted-content": "article",
    "monograph": "book",
    "proceedings-article": "paper-conference",
}

def fetch_doi(doi: str) -> dict | None:
    """Fetch a record from CrossRef by DOI.

    Returns a CSL-JSON dict on success and None when CrossRef says the DOI is
    not theirs (404). Anything that stops us getting an answer — rate limit,
    server error, timeout — raises ``net.SearchUnavailable`` rather than
    masquerading as a miss. Does NOT add source/source_id — the orchestrator
    does that.
    """
    response = net.get(f"https://api.crossref.org/works/{doi}", source="crossref")
    if response.status_code == 404:
        return None

    message = net.parse_json(response, "crossref").get("message", {})
    return _parse_message(message)


def _parse_message(msg: dict) -> dict:
    """Convert a CrossRef `message` dict into a CSL-JSON dict."""
    record: dict = {}

    # title: list -> first string
    title_list = msg.get("title") or []
    if title_list:
        record["title"] = title_list[0]

    # container-title: list -> first string
    ct_list = msg.get("container-title") or []
    if ct_list:
        record["container-title"] = ct_list[0]

    # author: keep family/given/literal, drop everything else
    raw_authors = msg.get("author") or []
    if raw_authors:
        authors = []
        for a in raw_authors:
            entry: dict = {}
            if "family" in a:
                entry["family"] = a["family"]
            if "given" in a:
                entry["given"] = a["given"]
            if "literal" in a and not entry:
                entry["literal"] = a["literal"]
            if entry:
                authors.append(entry)
        if authors:
            record["author"] = authors

    # issued: keep date-parts as-is
    issued = msg.get("issued")
    if issued and "date-parts" in issued:
        record["issued"] = {"date-parts": issued["date-parts"]}

    # simple scalar fields
    for key in ("DOI", "publisher", "URL", "page", "volume", "issue"):
        val = msg.get(key)
        if val is not None:
            record[key] = val

    # type mapping
    raw_type = msg.get("type") or ""
    record["type"] = _CROSSREF_TYPE_MAP.get(raw_type, raw_type)

    return record
