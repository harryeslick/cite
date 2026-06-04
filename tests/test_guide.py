"""Tests for cite.guide."""

import json

from cite.guide import guide
from cite.models import CITE_TYPES


class TestGuideJson:
    def test_parses_as_json(self):
        result = guide(as_json=True)
        data = json.loads(result)  # must not raise
        assert isinstance(data, dict)

    def test_has_required_top_level_keys(self):
        data = json.loads(guide(as_json=True))
        assert "workflow" in data
        assert "commands" in data
        assert "types" in data
        assert "notes" in data

    def test_types_has_seven_entries(self):
        data = json.loads(guide(as_json=True))
        assert len(data["types"]) == 7

    def test_types_matches_cite_types(self):
        data = json.loads(guide(as_json=True))
        type_names = [t["cite_type"] for t in data["types"]]
        for ct in CITE_TYPES:
            assert ct in type_names

    def test_each_type_has_required_fields(self):
        data = json.loads(guide(as_json=True))
        for t in data["types"]:
            assert "required_fields" in t
            assert isinstance(t["required_fields"], list)
            assert len(t["required_fields"]) > 0

    def test_each_type_has_csl_type_and_genre(self):
        data = json.loads(guide(as_json=True))
        for t in data["types"]:
            assert "csl_type" in t
            assert "genre" in t  # may be None

    def test_one_of_groups_rendered_with_pipe(self):
        data = json.loads(guide(as_json=True))
        # book has ("author", "editor") -> should appear as "author|editor"
        book = next(t for t in data["types"] if t["cite_type"] == "book")
        assert any("|" in f for f in book["required_fields"])

    def test_workflow_is_ordered_list(self):
        data = json.loads(guide(as_json=True))
        assert isinstance(data["workflow"], list)
        assert len(data["workflow"]) >= 3

    def test_commands_have_name_and_summary(self):
        data = json.loads(guide(as_json=True))
        for cmd in data["commands"]:
            assert "name" in cmd
            assert "summary" in cmd


class TestGuidePlainText:
    def test_returns_non_empty_string(self):
        result = guide(as_json=False)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mentions_cite_add(self):
        result = guide(as_json=False)
        assert "cite add" in result

    def test_mentions_provenance_key(self):
        result = guide(as_json=False)
        assert "_provenance" in result

    def test_mentions_all_cite_types(self):
        result = guide(as_json=False)
        for ct in CITE_TYPES:
            assert ct in result
