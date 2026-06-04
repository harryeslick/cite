"""Tests for the cite.search subpackage.

All HTTP calls are mocked with respx — no real network traffic.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from cite.search import crossref, datacite, openalex
from cite.search import search


# ---------------------------------------------------------------------------
# Sample payloads (small but realistic)
# ---------------------------------------------------------------------------

CROSSREF_PAYLOAD = {
    "status": "ok",
    "message-type": "work",
    "message": {
        "DOI": "10.1000/xyz123",
        "title": ["A Great Study of Things"],
        "container-title": ["Journal of Things"],
        "author": [
            {"family": "Smith", "given": "J.", "sequence": "first", "affiliation": []},
            {"family": "Jones", "given": "A.", "sequence": "additional", "affiliation": []},
        ],
        "issued": {"date-parts": [[2021, 3, 15]]},
        "publisher": "Acme Press",
        "URL": "https://doi.org/10.1000/xyz123",
        "volume": "12",
        "issue": "3",
        "page": "100-120",
        "type": "journal-article",
    },
}

DATACITE_PAYLOAD = {
    "data": {
        "id": "10.5281/zenodo.999",
        "type": "dois",
        "attributes": {
            "doi": "10.5281/zenodo.999",
            "titles": [{"title": "My Dataset"}],
            "creators": [
                {"familyName": "Brown", "givenName": "C.", "name": "Brown, C."},
                {"name": "ACME Corp"},  # no split name
            ],
            "publicationYear": 2022,
            "publisher": "Zenodo",
            "types": {"resourceTypeGeneral": "Dataset"},
            "url": "https://zenodo.org/record/999",
        },
    }
}

OPENALEX_PAYLOAD = {
    "meta": {"count": 1, "per_page": 5, "page": 1},
    "results": [
        {
            "id": "https://openalex.org/W123456",
            "display_name": "Reactive Oxygen Species in Cells",
            "publication_year": 2020,
            "doi": "https://doi.org/10.9999/ros2020",
            "type": "article",
            "authorships": [
                {"author": {"display_name": "Alice Wonderland"}},
                {"author": {"display_name": "Bob Builder"}},
            ],
            "primary_location": {
                "source": {"display_name": "Cell Biology Reports"}
            },
        }
    ],
}

OPENALEX_EMPTY = {"meta": {"count": 0, "per_page": 5, "page": 1}, "results": []}


# ---------------------------------------------------------------------------
# crossref.fetch_doi
# ---------------------------------------------------------------------------


@respx.mock
def test_crossref_fetch_doi_parses_correctly():
    respx.get("https://api.crossref.org/works/10.1000/xyz123").mock(
        return_value=httpx.Response(200, json=CROSSREF_PAYLOAD)
    )
    result = crossref.fetch_doi("10.1000/xyz123")

    assert result is not None
    # title must be a string, not a list
    assert isinstance(result["title"], str)
    assert result["title"] == "A Great Study of Things"
    # container-title must be a string, not a list
    assert result["container-title"] == "Journal of Things"
    # author preserved: 2 entries with family/given, no extra keys
    assert len(result["author"]) == 2
    assert result["author"][0] == {"family": "Smith", "given": "J."}
    assert result["author"][1] == {"family": "Jones", "given": "A."}
    # issued year correct
    assert result["issued"] == {"date-parts": [[2021, 3, 15]]}
    # type mapped: journal-article -> article-journal
    assert result["type"] == "article-journal"
    # scalar fields preserved
    assert result["DOI"] == "10.1000/xyz123"
    assert result["publisher"] == "Acme Press"
    assert result["volume"] == "12"
    assert result["issue"] == "3"
    assert result["page"] == "100-120"


@respx.mock
def test_crossref_fetch_doi_returns_none_on_404():
    respx.get("https://api.crossref.org/works/10.0000/notfound").mock(
        return_value=httpx.Response(404)
    )
    assert crossref.fetch_doi("10.0000/notfound") is None


@respx.mock
def test_crossref_type_mapping_book_chapter():
    payload = {
        "status": "ok",
        "message": {
            "DOI": "10.1000/ch1",
            "title": ["Chapter One"],
            "type": "book-chapter",
        },
    }
    respx.get("https://api.crossref.org/works/10.1000/ch1").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = crossref.fetch_doi("10.1000/ch1")
    assert result is not None
    assert result["type"] == "chapter"


# ---------------------------------------------------------------------------
# datacite.fetch_doi
# ---------------------------------------------------------------------------


@respx.mock
def test_datacite_fetch_doi_parses_correctly():
    respx.get("https://api.datacite.org/dois/10.5281/zenodo.999").mock(
        return_value=httpx.Response(200, json=DATACITE_PAYLOAD)
    )
    result = datacite.fetch_doi("10.5281/zenodo.999")

    assert result is not None
    assert result["title"] == "My Dataset"
    # type: Dataset -> dataset
    assert result["type"] == "dataset"
    # authors: 2 entries — first has family/given, second has literal
    assert len(result["author"]) == 2
    assert result["author"][0] == {"family": "Brown", "given": "C."}
    assert result["author"][1] == {"literal": "ACME Corp"}
    # issued year
    assert result["issued"] == {"date-parts": [[2022]]}
    assert result["publisher"] == "Zenodo"
    assert result["DOI"] == "10.5281/zenodo.999"
    assert result["URL"] == "https://zenodo.org/record/999"


@respx.mock
def test_datacite_fetch_doi_returns_none_on_404():
    respx.get("https://api.datacite.org/dois/10.0000/missing").mock(
        return_value=httpx.Response(404)
    )
    assert datacite.fetch_doi("10.0000/missing") is None


@respx.mock
def test_datacite_non_dataset_type_is_report():
    payload = {
        "data": {
            "attributes": {
                "doi": "10.9999/rep1",
                "titles": [{"title": "A Report"}],
                "creators": [],
                "publicationYear": 2023,
                "publisher": "Gov",
                "types": {"resourceTypeGeneral": "Text"},
                "url": "https://example.gov/rep1",
            }
        }
    }
    respx.get("https://api.datacite.org/dois/10.9999/rep1").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = datacite.fetch_doi("10.9999/rep1")
    assert result is not None
    assert result["type"] == "report"


# ---------------------------------------------------------------------------
# openalex.search
# ---------------------------------------------------------------------------


@respx.mock
def test_openalex_search_parses_correctly():
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=OPENALEX_PAYLOAD)
    )
    results = openalex.search("Reactive Oxygen Species in Cells")

    assert len(results) == 1
    r = results[0]
    assert r["title"] == "Reactive Oxygen Species in Cells"
    # DOI prefix stripped
    assert r["DOI"] == "10.9999/ros2020"
    assert r["issued"] == {"date-parts": [[2020]]}
    assert r["container-title"] == "Cell Biology Reports"
    # type mapped: article -> article-journal
    assert r["type"] == "article-journal"
    # author splitting: "Alice Wonderland" -> family=Wonderland, given=Alice
    assert r["author"][0]["family"] == "Wonderland"
    assert r["author"][0]["given"] == "Alice"
    assert r["author"][1]["family"] == "Builder"
    assert r["author"][1]["given"] == "Bob"
    # openalex_id present
    assert r["openalex_id"] == "https://openalex.org/W123456"


@respx.mock
def test_openalex_search_returns_empty_on_no_results():
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=OPENALEX_EMPTY)
    )
    results = openalex.search("nonexistent title nobody wrote")
    assert results == []


# ---------------------------------------------------------------------------
# search() orchestrator
# ---------------------------------------------------------------------------


@respx.mock
def test_orchestrator_doi_path_crossref_hit():
    respx.get("https://api.crossref.org/works/10.1000/xyz123").mock(
        return_value=httpx.Response(200, json=CROSSREF_PAYLOAD)
    )
    result = search(doi="10.1000/xyz123")

    assert result["status"] == "ok"
    assert result["query"]["doi"] == "10.1000/xyz123"
    assert len(result["candidates"]) == 1
    c = result["candidates"][0]
    assert c["source"] == "crossref"
    assert c["source_id"] == "10.1000/xyz123"
    assert "_cite_type" in c
    assert c["_cite_type"] == "journal-article"
    assert "suggested_next" in result


@respx.mock
def test_orchestrator_doi_crossref_404_falls_back_to_datacite():
    respx.get("https://api.crossref.org/works/10.5281/zenodo.999").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.datacite.org/dois/10.5281/zenodo.999").mock(
        return_value=httpx.Response(200, json=DATACITE_PAYLOAD)
    )
    result = search(doi="10.5281/zenodo.999")

    assert result["status"] == "ok"
    assert len(result["candidates"]) == 1
    c = result["candidates"][0]
    assert c["source"] == "datacite"
    assert c["source_id"] == "10.5281/zenodo.999"
    assert "_cite_type" in c
    assert c["_cite_type"] == "data-set"


@respx.mock
def test_orchestrator_title_path_returns_openalex_candidates():
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=OPENALEX_PAYLOAD)
    )
    result = search(title="Reactive Oxygen Species in Cells", year=2020)

    assert result["status"] == "ok"
    assert result["query"]["title"] == "Reactive Oxygen Species in Cells"
    assert result["query"]["year"] == 2020
    assert len(result["candidates"]) == 1
    c = result["candidates"][0]
    assert c["source"] == "openalex"
    # source_id should be the DOI when available
    assert c["source_id"] == "10.9999/ros2020"
    assert "_cite_type" in c
    assert c["_cite_type"] == "journal-article"
    # openalex_id should have been consumed and removed
    assert "openalex_id" not in c


@respx.mock
def test_orchestrator_empty_result_both_fail():
    respx.get("https://api.crossref.org/works/10.0000/nothing").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.datacite.org/dois/10.0000/nothing").mock(
        return_value=httpx.Response(404)
    )
    result = search(doi="10.0000/nothing")

    assert result["status"] == "empty"
    assert result["candidates"] == []
    assert "no match found" in result["suggested_next"]


@respx.mock
def test_orchestrator_empty_title_search():
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=OPENALEX_EMPTY)
    )
    result = search(title="this title will never exist in any database ever")

    assert result["status"] == "empty"
    assert result["candidates"] == []
    assert "no match found" in result["suggested_next"]
