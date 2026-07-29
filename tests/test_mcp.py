"""Tests for the thin MCP wrapper (`cite.mcp`).

The MCP tools call the same `cite.ops` functions as the CLI and JSON-encode the
returned envelopes, so these are fast, offline wrapper tests for direct
in-process behavior and the `library` override. They require the `mcp` extra —
skipped if it isn't installed.
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
    """An initialized but empty library reports zero references."""
    lib = str(tmp_path / "lib")
    cite_mcp.init(library=lib, yes=True)
    out = json.loads(cite_mcp.list_refs(library=lib))
    assert out == {"count": 0, "references": [], "spec": "suite/1"}


def test_reads_against_a_missing_library_say_so(tmp_path):
    """A non-existent root must not masquerade as an empty library.

    This is the failure this guard exists for: `list`/`get`/`doctor` against a
    path with no cite.toml used to return an empty success, making "you are
    pointed at the wrong directory" indistinguishable from "nothing is filed yet".
    """
    missing = str(tmp_path / "nope")
    for out in (
        json.loads(cite_mcp.list_refs(library=missing)),
        json.loads(cite_mcp.get("anything", library=missing)),
        json.loads(cite_mcp.doctor(library=missing)),
    ):
        assert out["status"] == "no_library"
        assert "cite init" in out["hint"]


def test_add_manual_then_list_then_remove(tmp_path):
    lib = str(tmp_path / "lib")
    assert json.loads(cite_mcp.init(library=lib, yes=True))["status"] == "created"
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


def test_update_patches_fields_through_the_cli(tmp_path):
    lib = str(tmp_path / "lib")
    assert json.loads(cite_mcp.init(library=lib, yes=True))["status"] == "created"
    src = tmp_path / "report.pdf"
    src.write_text("internal report contents")
    rid = json.loads(
        cite_mcp.add_manual(
            file=str(src),
            type="other-report",
            fields={"title": "Draft", "publisher": "Agency", "issued": "2022"},
            library=lib,
        )
    )["id"]

    # A non-id field edit keeps the id; the fields dict expands to --field args.
    out = json.loads(cite_mcp.update(rid, fields={"publisher": "Final Agency"}, library=lib))
    assert out["status"] == "updated"
    assert out["renamed"] is False
    assert out["record"]["publisher"] == "Final Agency"

    # An id-bearing edit re-stems the bundle, so the returned id differs.
    out = json.loads(cite_mcp.update(rid, fields={"title": "Final Report"}, library=lib))
    assert out["status"] == "updated"
    assert out["renamed"] is True
    assert out["id"] != rid


def test_unknown_record_is_not_found(tmp_path):
    """A real library that simply lacks the id — distinct from a missing library."""
    lib = str(tmp_path / "lib")
    cite_mcp.init(library=lib, yes=True)
    out = json.loads(cite_mcp.get("does-not-exist", library=lib))
    assert out["status"] == "not_found"


def test_self_check_reports_the_install(tmp_path):
    out = json.loads(cite_mcp.doctor(library=str(tmp_path / "nope"), self_check=True))
    assert out["extras"].keys() == {"mcp", "extract"}
    assert out["extras"]["mcp"] is True  # this test only runs with the extra
    assert out["library"]["initialized"] is False
    assert any("no library" in h for h in out["hints"])


def test_bad_input_returns_a_clean_error_envelope():
    """Malformed input to a tool comes back as a `{"status": "error"}` envelope
    with a one-line message — not a traceback. ops raises ValueError (a
    JSONDecodeError here) for bad input; the MCP shell catches it and returns the
    structured envelope directly (no subprocess, no Rich-traceback workaround).
    """
    out = json.loads(cite_mcp.validate("book", "not valid json"))
    assert out["status"] == "error"
    # The real parse error message survives, on a single clean line...
    assert out["message"]
    # ...with none of the traceback noise (box-art, frames, locals).
    assert "─" not in out["message"]  # Rich box-drawing char
    assert "Traceback" not in out["message"]
    assert "\n" not in out["message"]
    assert len(out["message"]) < 500
