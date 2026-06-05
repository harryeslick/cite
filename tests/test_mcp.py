"""Tests for the thin MCP wrapper (`cite.mcp`).

The MCP tools shell out to the real `cite` binary (installed on PATH by
`uv sync`/`uv tool install`), so these are fast, offline integration tests that
verify argv construction, stdin piping, and the `--library` override pass through
correctly. They require the `mcp` extra — skipped if it isn't installed.
"""

import json

import pytest

pytest.importorskip("mcp")  # the [mcp] extra; install with `uv sync --extra mcp`

from cite import mcp as cite_mcp  # noqa: E402


def test_guide_returns_the_cli_contract():
    data = json.loads(cite_mcp.guide())
    assert {"workflow", "commands", "types", "notes"} <= data.keys()
    assert len(data["types"]) == 7


def test_validate_reports_missing_fields():
    out = json.loads(cite_mcp.validate("journal-article", "{}"))
    assert out["status"] == "missing_fields"
    assert out["missing"]  # non-empty list of required fields


def test_list_empty_library(tmp_path):
    out = json.loads(cite_mcp.list_refs(library=str(tmp_path / "lib")))
    assert out == {"count": 0, "references": [], "spec": "suite/1"}


def test_add_manual_then_list_then_remove(tmp_path):
    lib = str(tmp_path / "lib")
    src = tmp_path / "report.pdf"
    src.write_text("internal report contents")

    added = json.loads(
        cite_mcp.add_manual(
            file=str(src),
            type="trial-report",
            fields={
                "title": "Phase II Trial of Widget X",
                "author": "Smith, Jane; Lee, Kim",
                "publisher": "Internal Lab",
                "issued": "2023",
            },
            library=lib,
        )
    )
    assert added["status"] == "added"
    rid = added["id"]

    listed = json.loads(cite_mcp.list_refs(library=lib))
    assert listed["count"] == 1

    removed = json.loads(cite_mcp.remove(rid, library=lib))
    assert removed["status"] == "removed"


def test_unknown_record_is_not_found(tmp_path):
    out = json.loads(cite_mcp.get("does-not-exist", library=str(tmp_path / "lib")))
    assert out["status"] == "not_found"
