"""CrossRef DOI lookup backend."""

from __future__ import annotations

import httpx

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

_USER_AGENT = "cite/0.1 (mailto:32809214+harryeslick@users.noreply.github.com)"


def fetch_doi(doi: str) -> dict | None:
    """Fetch a record from CrossRef by DOI.

    Returns a CSL-JSON dict on success, None on 404 or network error.
    Does NOT add source/source_id — the orchestrator does that.
    """
    url = f"https://api.crossref.org/works/{doi}"
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=15.0,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return None

    if response.status_code == 404:
        return None
    if not response.is_success:
        return None

    try:
        data = response.json()
    except Exception:
        return None

    message = data.get("message", {})
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
