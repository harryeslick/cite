"""Tests for the cite.search subpackage.

All HTTP calls are mocked with respx — no real network traffic.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from cite.search import crossref, datacite, net, openalex
from cite.search import search
from cite.store import Library


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

    assert result["status"] == "not_found"
    assert result["candidates"] == []
    assert "no match found" in result["suggested_next"]


@respx.mock
def test_orchestrator_empty_title_search():
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=OPENALEX_EMPTY)
    )
    result = search(title="this title will never exist in any database ever")

    assert result["status"] == "not_found"
    assert result["candidates"] == []
    assert "no match found" in result["suggested_next"]


@respx.mock
def test_orchestrator_title_candidates_are_compact_summaries():
    """A relevant title hit is returned as a pick-list summary, not full CSL."""
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=OPENALEX_PAYLOAD)
    )
    result = search(title="Reactive Oxygen Species in Cells")

    assert result["status"] == "ok"
    c = result["candidates"][0]
    # Compact, non-CSL shape: author array collapsed to a display string, issued
    # collapsed to a plain year. (Filing re-fetches the full record by DOI.)
    assert c["authors"] == "Wonderland, Alice et al. (2)"
    assert c["year"] == 2020
    assert "author" not in c  # full CSL author array dropped
    assert "issued" not in c
    # Still carries everything needed to file by DOI.
    assert c["source_id"] == "10.9999/ros2020"
    assert c["_cite_type"] == "journal-article"
    assert "--doi" in result["suggested_next"]


@respx.mock
def test_orchestrator_title_filters_irrelevant_hits_as_weak_match():
    """OpenAlex hits that don't match the query title are suppressed, not dumped."""
    junk = {
        "results": [
            {
                "id": "https://openalex.org/W1",
                "display_name": (
                    "Recommendations for Cardiac Chamber Quantification by "
                    "Echocardiography in Adults"
                ),
                "publication_year": 2015,
                "doi": "https://doi.org/10.1016/j.echo.2014.10.003",
                "type": "article",
                "authorships": [{"author": {"display_name": "Roberto Lang"}}],
            }
        ]
    }
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=junk)
    )
    result = search(title="Pulse Variety Disease Guide 2026")

    assert result["status"] == "weak_match"
    assert result["candidates"] == []
    assert "none matched" in result["note"]
    assert "--manual" in result["suggested_next"]


# ---------------------------------------------------------------------------
# Failure is not absence
#
# The regression these guard: every backend used to collapse 429s, 5xx and
# timeouts into the same sentinel as "no such record", so a rate-limited lookup
# reached the agent as "no match found — enter it by hand".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(503),
    ],
)
@respx.mock
def test_crossref_raises_rather_than_reporting_a_miss(response):
    respx.get("https://api.crossref.org/works/10.1000/xyz123").mock(
        return_value=response
    )
    with pytest.raises(net.SearchUnavailable) as excinfo:
        crossref.fetch_doi("10.1000/xyz123")
    assert excinfo.value.source == "crossref"


@respx.mock
def test_datacite_raises_on_connection_error():
    respx.get("https://api.datacite.org/dois/10.5281/zenodo.999").mock(
        side_effect=httpx.ConnectError("boom")
    )
    with pytest.raises(net.SearchUnavailable) as excinfo:
        datacite.fetch_doi("10.5281/zenodo.999")
    assert excinfo.value.kind == net.NETWORK_ERROR


@respx.mock
def test_openalex_raises_on_rate_limit_carrying_retry_after():
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "42"})
    )
    with pytest.raises(net.SearchUnavailable) as excinfo:
        openalex.search("Reactive Oxygen Species in Cells")
    assert excinfo.value.kind == net.RATE_LIMITED
    assert excinfo.value.retry_after == 42


@respx.mock
def test_openalex_raises_on_unreadable_body():
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, text="<html>gateway error</html>")
    )
    with pytest.raises(net.SearchUnavailable):
        openalex.search("anything")


@respx.mock
def test_orchestrator_title_rate_limit_is_unavailable_not_not_found():
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"})
    )
    result = search(title="Reactive Oxygen Species in Cells")

    assert result["status"] == "unavailable"
    assert result["candidates"] == []
    # The failure survives into the envelope, actionably.
    assert result["unavailable_sources"] == [
        {
            "source": "openalex",
            "kind": net.RATE_LIMITED,
            "reason": "openalex rate limit reached",
            "retry_after": 30,
        }
    ]
    # And the agent is told to retry, never to hand-key the record.
    assert "30s" in result["suggested_next"]
    assert "--manual" not in result["suggested_next"]


@respx.mock
def test_orchestrator_doi_all_sources_unavailable():
    respx.get("https://api.crossref.org/works/10.1000/xyz123").mock(
        return_value=httpx.Response(500)
    )
    respx.get("https://api.datacite.org/dois/10.1000/xyz123").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    result = search(doi="10.1000/xyz123")

    assert result["status"] == "unavailable"
    assert {e["source"] for e in result["unavailable_sources"]} == {
        "crossref",
        "datacite",
    }


@respx.mock
def test_orchestrator_doi_partial_failure_reports_the_gap():
    """CrossRef down, DataCite answers: `ok`, but say what we couldn't ask."""
    respx.get("https://api.crossref.org/works/10.5281/zenodo.999").mock(
        return_value=httpx.Response(429)
    )
    respx.get("https://api.datacite.org/dois/10.5281/zenodo.999").mock(
        return_value=httpx.Response(200, json=DATACITE_PAYLOAD)
    )
    result = search(doi="10.5281/zenodo.999")

    assert result["status"] == "ok"
    assert result["candidates"][0]["source"] == "datacite"
    assert result["unavailable_sources"][0]["source"] == "crossref"


@respx.mock
def test_orchestrator_genuine_miss_still_carries_no_failure_noise():
    respx.get("https://api.crossref.org/works/10.0000/nothing").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.datacite.org/dois/10.0000/nothing").mock(
        return_value=httpx.Response(404)
    )
    result = search(doi="10.0000/nothing")

    assert result["status"] == "not_found"
    assert "unavailable_sources" not in result


# ---------------------------------------------------------------------------
# Caller identity (net.py)
# ---------------------------------------------------------------------------


def test_user_agent_uses_contact_email_when_set(monkeypatch):
    monkeypatch.setenv(net.CONTACT_EMAIL_ENV, "someone@example.org")
    assert "mailto:someone@example.org" in net.user_agent()


def test_user_agent_falls_back_to_project_not_a_person(monkeypatch):
    monkeypatch.delenv(net.CONTACT_EMAIL_ENV, raising=False)
    agent = net.user_agent()
    assert "mailto:" not in agent
    assert agent.startswith("cite/")


@respx.mock
def test_openalex_sends_api_key_when_configured(monkeypatch):
    monkeypatch.setenv(net.OPENALEX_API_KEY_ENV, "sekrit")
    route = respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=OPENALEX_PAYLOAD)
    )
    openalex.search("Reactive Oxygen Species in Cells")
    assert route.calls.last.request.url.params["api_key"] == "sekrit"


@respx.mock
def test_openalex_search_works_without_an_api_key(monkeypatch):
    monkeypatch.delenv(net.OPENALEX_API_KEY_ENV, raising=False)
    route = respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=OPENALEX_PAYLOAD)
    )
    results = openalex.search("Reactive Oxygen Species in Cells")
    assert len(results) == 1
    assert "api_key" not in route.calls.last.request.url.params


# ---------------------------------------------------------------------------
# Identity from cite.toml
# ---------------------------------------------------------------------------


def _library_with_config(tmp_path, body: str) -> Library:
    """A library whose cite.toml carries `body`, wired in as the resolved one."""
    lib = Library(tmp_path / "lib")
    lib.init()
    (lib.root / "cite.toml").write_text(body, encoding="utf-8")
    return lib


def test_settings_read_from_cite_toml(tmp_path, monkeypatch):
    lib = _library_with_config(
        tmp_path,
        '[search]\ncontact_email = "lib@example.org"\nopenalex_api_key = "from-toml"\n',
    )
    monkeypatch.setattr(net, "resolve_library", lambda _: lib)

    assert net.contact_email() == "lib@example.org"
    assert net.openalex_api_key() == "from-toml"
    assert net.identity_report()["openalex_api_key_source"] == "cite.toml"


def test_env_overrides_cite_toml(tmp_path, monkeypatch):
    lib = _library_with_config(
        tmp_path, '[search]\nopenalex_api_key = "from-toml"\n'
    )
    monkeypatch.setattr(net, "resolve_library", lambda _: lib)
    monkeypatch.setenv(net.OPENALEX_API_KEY_ENV, "from-env")

    assert net.openalex_api_key() == "from-env"
    assert net.identity_report()["openalex_api_key_source"] == "env"


def test_a_library_without_a_search_section_is_simply_unconfigured(tmp_path, monkeypatch):
    """`cite init` writes the section commented out — that must parse to nothing."""
    lib = Library(tmp_path / "lib")
    lib.init()
    monkeypatch.setattr(net, "resolve_library", lambda _: lib)

    assert lib.config().get("search") is None
    assert net.openalex_api_key() is None
    assert net.contact_email() is None


def test_a_malformed_cite_toml_does_not_break_searching(tmp_path, monkeypatch):
    """Optional settings must never be able to take down a search."""
    lib = _library_with_config(tmp_path, "[search\nthis is not toml")
    monkeypatch.setattr(net, "resolve_library", lambda _: lib)

    assert lib.config() == {}
    assert net.openalex_api_key() is None


def test_identity_report_never_echoes_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv(net.OPENALEX_API_KEY_ENV, "super-secret")
    report = net.identity_report()

    assert report["openalex_api_key"] is True
    assert "super-secret" not in str(report)


@respx.mock
def test_openalex_uses_a_key_configured_only_in_cite_toml(tmp_path, monkeypatch):
    """The whole point of the config file: no env wiring, key still sent."""
    lib = _library_with_config(tmp_path, '[search]\nopenalex_api_key = "toml-key"\n')
    monkeypatch.setattr(net, "resolve_library", lambda _: lib)
    route = respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=OPENALEX_PAYLOAD)
    )
    openalex.search("Reactive Oxygen Species in Cells")

    assert route.calls.last.request.url.params["api_key"] == "toml-key"
