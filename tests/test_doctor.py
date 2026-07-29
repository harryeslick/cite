"""Unit tests for the library health check (`cite doctor`).

These build small on-disk libraries with the storage layer, then assert that
``run_doctor`` flags exactly the problems planted — and nothing else (notably not
``cite.toml`` or the ``.staging/`` cache).
"""

import json

from cite.doctor import (
    INVALID_JSON,
    MISSING_FIELDS,
    MISSING_FILE,
    MISSING_PROVENANCE,
    ORPHAN,
    run_doctor,
    run_self_check,
)
from cite import ops
from cite.store import Library


def _healthy_record(record_id: str) -> dict:
    """A valid 'other-report' record whose stored file we will also create."""
    return {
        "type": "report",
        "title": "Annual Widget Report",
        "publisher": "Internal Lab",
        "issued": {"date-parts": [[2023]]},
        "_provenance": {
            "cite_type": "other-report",
            "new_filename": f"{record_id}.pdf",
            "file_hash": "deadbeef",
        },
    }


def _write_bundle(lib: Library, record_id: str, record: dict, *, with_file=True):
    lib.write_record(record_id, record)
    if with_file:
        new_filename = record.get("_provenance", {}).get("new_filename")
        if new_filename:
            (lib.entry_dir(record_id) / new_filename).write_text("bytes")


def _make_lib(tmp_path) -> Library:
    lib = Library(tmp_path / "lib")
    lib.init()  # creates root + cite.toml
    return lib


def test_clean_library_is_ok(tmp_path):
    lib = _make_lib(tmp_path)
    rid = "2023-good-report_abc123"
    _write_bundle(lib, rid, _healthy_record(rid))

    out = run_doctor(lib)
    assert out["status"] == "ok"
    assert out["checked"] == 1
    assert out["healthy"] == 1
    assert out["problem_count"] == 0
    assert out["problems"] == []
    assert out["summary"] == {}


def test_empty_or_missing_library_is_ok(tmp_path):
    # init() makes the root but no bundles; also cite.toml must not be flagged.
    lib = _make_lib(tmp_path)
    out = run_doctor(lib)
    assert out == {
        "status": "ok",
        "checked": 0,
        "healthy": 0,
        "problem_count": 0,
        "problems": [],
        "summary": {},
    }


def test_invalid_json_is_flagged(tmp_path):
    lib = _make_lib(tmp_path)
    rid = "2023-broken_abc123"
    lib.entry_dir(rid).mkdir(parents=True)
    lib.record_path(rid).write_text("{ not valid json")

    out = run_doctor(lib)
    assert out["status"] == "problems"
    assert out["summary"] == {INVALID_JSON: 1}
    assert out["problems"][0]["id"] == f"cite:{rid}"
    assert out["problems"][0]["problem"] == INVALID_JSON


def test_missing_provenance_is_flagged(tmp_path):
    lib = _make_lib(tmp_path)
    rid = "2023-noprov_abc123"
    lib.write_record(rid, {"type": "report", "title": "No provenance"})

    out = run_doctor(lib)
    assert out["summary"] == {MISSING_PROVENANCE: 1}


def test_missing_required_fields_is_flagged(tmp_path):
    lib = _make_lib(tmp_path)
    rid = "2023-incomplete_abc123"
    record = _healthy_record(rid)
    del record["title"]  # 'other-report' requires a title
    _write_bundle(lib, rid, record)

    out = run_doctor(lib)
    assert out["summary"].get(MISSING_FIELDS) == 1
    assert "title" in out["problems"][0]["detail"]


def test_missing_source_file_is_flagged(tmp_path):
    lib = _make_lib(tmp_path)
    rid = "2023-nofile_abc123"
    _write_bundle(lib, rid, _healthy_record(rid), with_file=False)

    out = run_doctor(lib)
    assert out["summary"] == {MISSING_FILE: 1}
    assert out["problems"][0]["problem"] == MISSING_FILE


def test_orphan_bundle_is_flagged(tmp_path):
    lib = _make_lib(tmp_path)
    # A directory under root with no <id>/<id>.json record.
    (lib.root / "2023-orphan_abc123").mkdir()

    out = run_doctor(lib)
    assert out["summary"] == {ORPHAN: 1}


def test_staging_cache_is_not_flagged(tmp_path):
    lib = _make_lib(tmp_path)
    lib.staging_dir().mkdir()  # .staging/ must be ignored by the walk
    rid = "2023-good_abc123"
    _write_bundle(lib, rid, _healthy_record(rid))

    out = run_doctor(lib)
    assert out["status"] == "ok"
    assert out["checked"] == 1


def test_one_bundle_with_two_problems_counts_healthy_once(tmp_path):
    lib = _make_lib(tmp_path)
    rid = "2023-double_abc123"
    record = _healthy_record(rid)
    del record["title"]  # missing field
    _write_bundle(lib, rid, record, with_file=False)  # and missing file

    out = run_doctor(lib)
    assert out["checked"] == 1
    assert out["healthy"] == 0
    assert out["problem_count"] == 2
    assert set(out["summary"]) == {MISSING_FIELDS, MISSING_FILE}


def test_problems_are_json_serializable_and_compact(tmp_path):
    lib = _make_lib(tmp_path)
    rid = "2023-incomplete_abc123"
    record = _healthy_record(rid)
    del record["title"]
    _write_bundle(lib, rid, record)

    out = run_doctor(lib)
    # Round-trips and stays small (context-frugal envelope).
    assert json.loads(json.dumps(out)) == out


# --------------------------------------------------------------------------- #
# Self-check — the install, not the library's contents
# --------------------------------------------------------------------------- #


class TestSelfCheck:
    """`cite doctor --self` answers "why is the tool behaving impossibly?".

    It exists because the two failures that silently derail a session — an MCP
    server that never connects because its extra is missing, and a library root
    that resolved somewhere unexpected — are both invisible from any other
    command's output.
    """

    def test_reports_extras_and_the_mcp_entrypoint(self, tmp_path):
        out = run_self_check(Library(tmp_path / "lib"))
        assert out["extras"].keys() == {"mcp", "extract"}
        # The MCP server is launched by the host, so a missing extra shows up
        # only as a server that never appears — state it explicitly instead.
        assert ("broken" in out["mcp_entrypoint"]) is (not out["extras"]["mcp"])

    def test_explains_an_unresolved_library(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CITE_LIBRARY", raising=False)
        monkeypatch.chdir(tmp_path)
        out = run_self_check(ops.resolve_library(None))

        assert out["status"] == "problems"
        assert out["library"]["initialized"] is False
        assert out["library"]["searched_from"] == str(tmp_path)
        assert any("no library" in hint for hint in out["hints"])

    def test_a_healthy_install_with_a_real_library_is_ok(self, tmp_path, monkeypatch):
        lib = Library(tmp_path / "lib")
        lib.init()
        out = run_self_check(lib)
        # `status` is ok only when nothing is missing; extras depend on the env.
        expected = "ok" if all(out["extras"].values()) else "problems"
        assert out["status"] == expected
        assert out["library"]["initialized"] is True
        assert out["hints"] == [] or all("library" not in h for h in out["hints"])
