"""Tests for cite.validate."""

import pytest

from cite.validate import validate


def _make_journal_article() -> dict:
    return {
        "type": "article-journal",
        "title": "Deep Learning for Citation Extraction",
        "author": [{"family": "Smith", "given": "Alice"}],
        "container-title": "Journal of AI Research",
        "issued": {"date-parts": [[2024]]},
    }


def _make_web_title_only() -> dict:
    return {
        "type": "webpage",
        "title": "My Web Page",
    }


class TestValidateOk:
    def test_complete_journal_article_returns_ok(self):
        result = validate("journal-article", _make_journal_article())
        assert result == {"status": "ok"}


class TestValidateMissingFields:
    def test_web_site_title_only_missing_url_and_accessed(self):
        result = validate("web-site", _make_web_title_only())
        assert result["status"] == "missing_fields"
        assert "URL" in result["missing"]
        assert "accessed" in result["missing"]

    def test_missing_fields_hint_contains_manual(self):
        result = validate("web-site", _make_web_title_only())
        assert "--manual" in result["hint"]

    def test_missing_fields_hint_lists_field_flags(self):
        result = validate("web-site", _make_web_title_only())
        for token in result["missing"]:
            assert f"--field {token}=..." in result["hint"]

    def test_missing_fields_result_has_cite_type(self):
        result = validate("web-site", _make_web_title_only())
        assert result["cite_type"] == "web-site"


class TestValidateUnknownType:
    def test_unknown_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown cite_type"):
            validate("nonsense-type", {})
