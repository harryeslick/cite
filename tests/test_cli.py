"""Integration tests for the CLI orchestrator (offline branches only).

These exercise the wiring in cli.py — provenance construction, the validation
gate, the dedup gate, and JSON output — without touching the network. The live
DOI path is covered by the search unit tests (mocked) plus manual smoke testing.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from cite import ops
from cite.cli import app

runner = CliRunner()


def _central_lib():
    """The central library handle (redirected by the isolate_central fixture)."""
    return ops.resolve_central()


def _run(args, input=None):
    result = runner.invoke(app, args, input=input)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _sample_file(tmp_path: Path) -> Path:
    f = tmp_path / "report.pdf"
    f.write_text("internal report contents")
    return f


def _other_file(tmp_path: Path, name: str = "rescan.pdf") -> Path:
    """A file with *different bytes* — a second copy/version of the same work."""
    f = tmp_path / name
    f.write_text(f"different bytes for {name}")
    return f


def _new_lib(tmp_path: Path, name: str = "lib") -> str:
    """Create and initialize a fresh library — `add`/`add-url` now require this."""
    lib = str(tmp_path / name)
    out = _run(["init", "--library", lib, "--yes"])
    assert out["status"] == "created", out
    return lib


def test_version_reports_package_version():
    from cite import __version__

    out = _run(["version"])
    assert out["status"] == "ok"
    assert out["tool"] == "cite"
    assert out["version"] == __version__
    assert isinstance(out["installed"], bool)
    # Each declared extra is reported as a name->bool availability map.
    assert set(out["extras"]) == {"mcp", "extract"}
    assert all(isinstance(v, bool) for v in out["extras"].values())


def test_guide_json_lists_seven_types():
    result = runner.invoke(app, ["guide", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data["types"]) == 7


def test_add_manual_missing_fields_returns_hint(tmp_path):
    f = _sample_file(tmp_path)
    lib = _new_lib(tmp_path)
    out = _run([
        "add", str(f), "--manual", "--type", "web-site",
        "--field", "title=Widget page",
        "--library", lib,
    ])
    assert out["status"] == "missing_fields"
    assert "URL" in out["missing"] and "accessed" in out["missing"]
    assert "--manual" in out["hint"]


def test_add_manual_unknown_type_returns_clean_error(tmp_path):
    """An invalid cite_type yields a one-line JSON error, not a Rich traceback."""
    f = _sample_file(tmp_path)
    lib = _new_lib(tmp_path)
    out = _run([
        "add", str(f), "--manual", "--type", "report",  # not in the vocabulary
        "--field", "title=Foo",
        "--library", lib,
    ])
    assert out["status"] == "error"
    assert "Unknown cite_type 'report'" in out["message"]
    # The whole envelope is tiny — no traceback frames or locals dump.
    assert len(json.dumps(out)) < 400


def test_add_manual_complete_then_duplicate_then_list(tmp_path):
    f = _sample_file(tmp_path)
    lib = _new_lib(tmp_path)
    common = [
        "add", str(f), "--manual", "--type", "trial-report",
        "--field", "title=Phase II Trial of Widget X",
        "--field", "author=Smith, Jane; Lee, Kim; Brown, Sam",
        "--field", "publisher=Internal Lab",
        "--field", "issued=2023",
        "--library", lib,
    ]
    added = _run(common)
    assert added["status"] == "added"
    assert added["record"]["type"] == "report"
    assert added["record"]["genre"] == "trial report"
    # 3 authors -> first author + 'etal'; ids are emitted namespaced (cite:<stem>)
    assert added["id"].startswith("cite:2023-smith-etal-")
    assert added["record"]["_provenance"]["source"] == "manual"

    # Same bytes -> dedup gate fires.
    dup = _run(common)
    assert dup["status"] == "duplicate"

    listing = _run(["list", "--library", lib])
    assert listing["count"] == 1
    assert listing["references"][0]["cite_type"] == "trial-report"


def test_validate_via_stdin(tmp_path):
    record = json.dumps({"title": "X"})
    out = _run(["validate", "--type", "web-site", "--csl", "-"], input=record)
    assert out["status"] == "missing_fields"
    assert set(out["missing"]) == {"URL", "accessed"}


def test_get_and_remove(tmp_path):
    f = _sample_file(tmp_path)
    lib = _new_lib(tmp_path)
    added = _run([
        "add", str(f), "--manual", "--type", "other-report",
        "--field", "title=Annual Report",
        "--field", "publisher=Agency",
        "--field", "issued=2022",
        "--library", lib,
    ])
    rid = added["id"]
    got = _run(["get", rid, "--library", lib])
    assert got["title"] == "Annual Report"

    removed = _run(["remove", rid, "--library", lib])
    assert removed["status"] == "removed"
    # The whole reference bundle is gone from disk.
    stem = rid.split(":", 1)[1]
    assert not (Path(lib) / stem).exists()


def test_id_is_namespaced_and_get_accepts_either_form(tmp_path):
    f = _sample_file(tmp_path)
    lib = _new_lib(tmp_path)
    added = _run([
        "add", str(f), "--manual", "--type", "other-report",
        "--field", "title=Annual Report",
        "--field", "publisher=Agency",
        "--field", "issued=2022",
        "--library", lib,
    ])
    rid = added["id"]
    assert rid.startswith("cite:")              # logical id is namespaced
    assert added["spec"] == "suite/1"           # envelope stamped with protocol

    # get accepts the bare stem too (namespace prefix is optional on input)...
    bare = rid.split(":", 1)[1]
    got = _run(["get", bare, "--library", lib])
    assert got["title"] == "Annual Report"
    # ...and the returned record is a raw payload, not an envelope (no spec stamp).
    assert "spec" not in got


def test_guide_advertises_spec_version():
    data = json.loads(runner.invoke(app, ["guide", "--json"]).output)
    assert data["spec"] == "suite/1"


# --------------------------------------------------------------------------- #
# Near-duplicate gate (metadata-level, layered on the exact-hash gate)
# --------------------------------------------------------------------------- #


def _add_report(file: Path, lib: str, *, title, author, year, doi=None, force=False):
    args = [
        "add", str(file), "--manual", "--type", "other-report",
        "--field", f"title={title}",
        "--field", f"author={author}",
        "--field", f"issued={year}",
        "--library", lib,
    ]
    if doi:
        args += ["--field", f"DOI={doi}"]
    if force:
        args.append("--force")
    return _run(args)


def test_near_duplicate_definitive_on_doi_then_force(tmp_path):
    lib = _new_lib(tmp_path)
    first = _add_report(
        _sample_file(tmp_path), lib,
        title="Soil Moisture Study", author="Smith, Jane", year=2023, doi="10.1/abc",
    )
    assert first["status"] == "added"

    # A *different file* with the same DOI is the same registered work.
    dup = _add_report(
        _other_file(tmp_path), lib,
        title="Soil Moisture Study (rescan)", author="Smith, Jane", year=2023,
        doi="10.1/ABC",  # case/prefix-insensitive DOI match
    )
    assert dup["status"] == "near_duplicate"
    assert dup["candidates"][0]["tier"] == "definitive"
    assert dup["candidates"][0]["id"] == first["id"]
    # Nothing committed — still one record.
    assert _run(["list", "--library", lib])["count"] == 1

    # The agent decides it's genuinely worth keeping and forces it through.
    forced = _add_report(
        _other_file(tmp_path), lib,
        title="Soil Moisture Study (rescan)", author="Smith, Jane", year=2023,
        doi="10.1/ABC", force=True,
    )
    assert forced["status"] == "added"
    assert _run(["list", "--library", lib])["count"] == 2


def test_near_duplicate_strong_on_title_author_year(tmp_path):
    lib = _new_lib(tmp_path)
    _add_report(
        _sample_file(tmp_path), lib,
        title="Annual Wheat Yield Report", author="Brown, Sam", year=2022,
    )
    dup = _add_report(
        _other_file(tmp_path), lib,
        title="Annual Wheat Yield Report", author="Brown, Sam", year=2022,
    )
    assert dup["status"] == "near_duplicate"
    assert dup["candidates"][0]["tier"] == "strong"


def test_exact_hash_gate_beats_near_duplicate_and_ignores_force(tmp_path):
    lib = _new_lib(tmp_path)
    f = _sample_file(tmp_path)
    _add_report(f, lib, title="Identical Report", author="Lee, Kim", year=2021)

    # Same bytes -> exact duplicate, even with --force (the bytes gate is absolute).
    dup = _add_report(
        f, lib, title="Identical Report", author="Lee, Kim", year=2021, force=True,
    )
    assert dup["status"] == "duplicate"


def test_unrelated_metadata_adds_cleanly(tmp_path):
    lib = _new_lib(tmp_path)
    _add_report(
        _sample_file(tmp_path), lib,
        title="Quantum Computing Basics", author="Feynman, R", year=2001,
    )
    out = _add_report(
        _other_file(tmp_path), lib,
        title="Medieval Crop Rotation", author="Smith, A", year=1995,
    )
    assert out["status"] == "added"


# --------------------------------------------------------------------------- #
# update — in-place metadata edit, with id re-stem when needed
# --------------------------------------------------------------------------- #


def _add_basic(tmp_path, lib, *, title="Annual Report", year=2022):
    """Add a minimal other-report and return its added envelope."""
    return _run([
        "add", str(_sample_file(tmp_path)), "--manual", "--type", "other-report",
        "--field", f"title={title}",
        "--field", "publisher=Agency",
        "--field", f"issued={year}",
        "--library", lib,
    ])


def test_update_non_id_field_keeps_id_and_rewrites_in_place(tmp_path):
    lib = _new_lib(tmp_path)
    rid = _add_basic(tmp_path, lib)["id"]

    out = _run(["update", rid, "--field", "publisher=New Agency", "--library", lib])
    assert out["status"] == "updated"
    assert out["renamed"] is False          # publisher is not an id-bearing field
    assert out["id"] == rid                  # id unchanged
    assert out["record"]["publisher"] == "New Agency"

    # The change is persisted and the content hash / document survive untouched.
    got = _run(["get", rid, "--library", lib])
    assert got["publisher"] == "New Agency"
    assert got["_provenance"]["file_hash"]   # provenance preserved


def test_update_title_restems_the_whole_bundle(tmp_path):
    lib = _new_lib(tmp_path)
    added = _add_basic(tmp_path, lib, title="Old Title")
    rid = added["id"]
    old_stem = rid.split(":", 1)[1]
    old_hash = added["record"]["_provenance"]["file_hash"]

    out = _run(["update", rid, "--field", "title=Brand New Title", "--library", lib])
    assert out["status"] == "updated"
    assert out["renamed"] is True
    assert out["old_id"] == rid
    new_stem = out["id"].split(":", 1)[1]
    assert "brand-new-title" in new_stem
    # The content-hash suffix is stable across the rename (identity preserved).
    assert new_stem.endswith(old_stem.rsplit("_", 1)[1])

    # On disk: the old bundle is gone, the new one holds re-stemmed files.
    assert not (Path(lib) / old_stem).exists()
    new_dir = Path(lib) / new_stem
    assert (new_dir / f"{new_stem}.json").exists()
    assert (new_dir / f"{new_stem}.pdf").exists()   # the document was re-stemmed

    # The record is reachable under the new id, with provenance updated to match.
    got = _run(["get", out["id"], "--library", lib])
    assert got["title"] == "Brand New Title"
    assert got["_provenance"]["file_hash"] == old_hash          # bytes unchanged
    assert got["_provenance"]["new_filename"] == f"{new_stem}.pdf"


def test_update_missing_field_rolls_back(tmp_path):
    lib = _new_lib(tmp_path)
    rid = _add_basic(tmp_path, lib)["id"]

    # other-report requires title; removing it must fail and commit nothing.
    out = _run(["update", rid, "--remove-field", "title", "--library", lib])
    assert out["status"] == "missing_fields"
    assert "title" in out["missing"]

    # The original record is untouched on disk.
    got = _run(["get", rid, "--library", lib])
    assert got["title"] == "Annual Report"


def test_update_type_change_rewrites_csl_type(tmp_path):
    lib = _new_lib(tmp_path)
    rid = _add_basic(tmp_path, lib)["id"]

    out = _run(["update", rid, "--type", "trial-report", "--library", lib])
    assert out["status"] == "updated"
    assert out["cite_type"] == "trial-report"
    assert out["record"]["type"] == "report"
    assert out["record"]["genre"] == "trial report"
    assert out["record"]["_provenance"]["cite_type"] == "trial-report"


def test_update_unknown_id_is_not_found(tmp_path):
    lib = _new_lib(tmp_path)
    out = _run(["update", "nope", "--field", "title=X", "--library", lib])
    assert out["status"] == "not_found"


def test_update_with_no_changes_is_an_error(tmp_path):
    lib = _new_lib(tmp_path)
    rid = _add_basic(tmp_path, lib)["id"]
    out = _run(["update", rid, "--library", lib])
    assert out["status"] == "error"
    assert "no changes" in out["message"]


# --------------------------------------------------------------------------- #
# Central library — smart add + auto-sync (U3)
# --------------------------------------------------------------------------- #


def test_add_auto_syncs_to_central(tmp_path):
    """AE2/AE3: a normal add creates the central library and copies the bundle in."""
    lib = _new_lib(tmp_path)
    central = _central_lib()
    assert not central.is_initialized()  # nothing yet

    added = _add_basic(tmp_path, lib)
    assert added["status"] == "added"
    assert "source" not in added  # normal pipeline, not a central import

    # Central was auto-created and now holds the same bundle.
    assert central.is_initialized()
    stem = added["id"].split(":", 1)[1]
    assert central.entry_dir(stem).is_dir()
    assert central.read_record(stem)["title"] == "Annual Report"


def test_add_imports_from_central_without_search(tmp_path, monkeypatch):
    """AE1: adding bytes already in central imports the bundle, skipping the pipeline."""
    # First add into project A populates central.
    lib_a = _new_lib(tmp_path, name="proj_a")
    file = _sample_file(tmp_path)
    first = _run([
        "add", str(file), "--manual", "--type", "other-report",
        "--field", "title=Shared Work",
        "--field", "publisher=Agency",
        "--field", "issued=2021",
        "--library", lib_a,
    ])
    assert first["status"] == "added"

    # Second add of the SAME bytes into project B: a DOI add would normally call
    # the search backend. Make search explode so a hit on central is provable —
    # if smart-add works, search is never reached.
    def _boom(**kwargs):
        raise AssertionError("search must not run on a central hit")

    monkeypatch.setattr("cite.ops.run_search", _boom)

    lib_b = _new_lib(tmp_path, name="proj_b")
    second = _run([
        "add", str(file), "--doi", "10.1/whatever", "--library", lib_b,
    ])
    assert second["status"] == "added"
    assert second["source"] == "central"
    assert second["id"] == first["id"]
    # The bundle really landed in project B.
    stem = second["id"].split(":", 1)[1]
    assert (Path(lib_b) / stem).is_dir()


def test_add_url_html_syncs_to_central(tmp_path, monkeypatch):
    """U3 scenario 5: the add-url HTML path also auto-syncs to central."""
    import cite.web as web

    class _Fetched:
        final_url = "https://example.org/page"
        content_type = "text/html"
        body = b"<html><head><title>Example Page</title></head><body>hi</body></html>"
        text = body.decode()

    monkeypatch.setattr(web, "fetch_url", lambda url: _Fetched())
    monkeypatch.setattr(web, "is_pdf", lambda f: False)
    monkeypatch.setattr(web, "is_html", lambda f: True)
    monkeypatch.setattr(
        web, "parse_webpage_metadata",
        lambda text, url: {"title": "Example Page", "URL": url, "type": "webpage"},
    )

    lib = _new_lib(tmp_path)
    out = _run(["add-url", "https://example.org/page", "--library", lib])
    assert out["status"] == "added"

    central = _central_lib()
    stem = out["id"].split(":", 1)[1]
    assert central.entry_dir(stem).is_dir()


def test_add_into_central_directly_is_noop_sync(tmp_path, monkeypatch):
    """AE7: when the project library IS the central library, sync is a no-op."""
    central = _central_lib()
    central.init()
    # Point --library at the central path itself.
    out = _run([
        "add", str(_sample_file(tmp_path)), "--manual", "--type", "other-report",
        "--field", "title=Direct Add",
        "--field", "publisher=Agency",
        "--field", "issued=2020",
        "--library", str(central.root),
    ])
    assert out["status"] == "added"
    assert "source" not in out  # not imported from itself
    stem = out["id"].split(":", 1)[1]
    # Exactly one bundle on disk (no self-copy duplicate).
    assert central.entry_dir(stem).is_dir()


# --------------------------------------------------------------------------- #
# Pull from central (U4)
# --------------------------------------------------------------------------- #


def test_pull_copies_from_central_to_project(tmp_path):
    """AE4: pull an existing central reference into a project."""
    lib_a = _new_lib(tmp_path, name="proj_a")
    added = _add_basic(tmp_path, lib_a)
    rid = added["id"]

    lib_b = _new_lib(tmp_path, name="proj_b")
    out = _run(["pull", rid, "--library", lib_b])
    assert out["status"] == "pulled"
    assert out["id"] == rid
    # Bundle really landed in project B.
    stem = rid.split(":", 1)[1]
    assert (Path(lib_b) / stem / f"{stem}.json").exists()


def test_pull_not_found_when_absent(tmp_path):
    lib = _new_lib(tmp_path)
    # Central is empty (auto-create hasn't fired), so init it manually.
    _central_lib().init()
    out = _run(["pull", "nonexistent", "--library", lib])
    assert out["status"] == "not_found"


def test_pull_duplicate_when_already_in_project(tmp_path):
    lib = _new_lib(tmp_path)
    added = _add_basic(tmp_path, lib)
    # The same bundle is now in both project and central.
    out = _run(["pull", added["id"], "--library", lib])
    assert out["status"] == "duplicate"


def test_pull_accepts_bare_and_namespaced_id(tmp_path):
    lib_a = _new_lib(tmp_path, name="proj_a")
    added = _add_basic(tmp_path, lib_a)
    bare = added["id"].split(":", 1)[1]

    lib_b = _new_lib(tmp_path, name="proj_b")
    out = _run(["pull", bare, "--library", lib_b])
    assert out["status"] == "pulled"


# --------------------------------------------------------------------------- #
# Update sync to central (U5)
# --------------------------------------------------------------------------- #


def test_update_syncs_metadata_to_central(tmp_path):
    """AE5: update in project propagates to central."""
    lib = _new_lib(tmp_path)
    added = _add_basic(tmp_path, lib)
    rid = added["id"]

    out = _run(["update", rid, "--field", "publisher=New Agency", "--library", lib])
    assert out["status"] == "updated"

    central = _central_lib()
    stem = out["id"].split(":", 1)[1]
    central_record = central.read_record(stem)
    assert central_record["publisher"] == "New Agency"


def test_update_title_restems_central_bundle(tmp_path):
    lib = _new_lib(tmp_path)
    added = _add_basic(tmp_path, lib, title="Old Title")
    old_stem = added["id"].split(":", 1)[1]

    out = _run(["update", added["id"], "--field", "title=Brand New Title", "--library", lib])
    assert out["status"] == "updated"
    assert out["renamed"] is True
    new_stem = out["id"].split(":", 1)[1]

    central = _central_lib()
    # Old central bundle gone, new one present with updated metadata.
    assert not central.entry_dir(old_stem).is_dir()
    assert central.entry_dir(new_stem).is_dir()
    assert central.read_record(new_stem)["title"] == "Brand New Title"


def test_update_when_not_in_central_is_silent(tmp_path):
    """Update a reference that only exists in the project (no central copy)."""
    lib = _new_lib(tmp_path)
    # Add directly to lib without central being initialized.
    central = _central_lib()
    assert not central.is_initialized()
    added = _add_basic(tmp_path, lib)

    # Central is now initialized (auto-create on add). Remove the central copy
    # to simulate a reference that was never in central.
    stem = added["id"].split(":", 1)[1]
    central.remove(stem)

    out = _run(["update", added["id"], "--field", "publisher=X", "--library", lib])
    assert out["status"] == "updated"
    # No crash — the update succeeds even though central has no copy.


# --------------------------------------------------------------------------- #
# Browse and remove central (U6)
# --------------------------------------------------------------------------- #


def test_list_central_shows_central_library(tmp_path):
    """R14: cite list --central lists the central library."""
    lib = _new_lib(tmp_path)
    _add_basic(tmp_path, lib, title="Annual Report")
    _add_report(
        _other_file(tmp_path), lib,
        title="Quantum Computing Fundamentals", author="Lee, Kim", year=2023,
    )

    out = _run(["list", "--central"])
    assert out["count"] == 2
    titles = {r["title"] for r in out["references"]}
    assert "Annual Report" in titles
    assert "Quantum Computing Fundamentals" in titles


def test_remove_is_project_local(tmp_path):
    """AE6: remove in project does not affect central."""
    lib = _new_lib(tmp_path)
    added = _add_basic(tmp_path, lib)
    rid = added["id"]
    stem = rid.split(":", 1)[1]

    _run(["remove", rid, "--library", lib])
    # Project bundle is gone.
    assert not (Path(lib) / stem).exists()
    # Central bundle is still there.
    central = _central_lib()
    assert central.entry_dir(stem).is_dir()


def test_remove_central_flag(tmp_path):
    """R15: cite remove --central removes from central only."""
    lib = _new_lib(tmp_path)
    added = _add_basic(tmp_path, lib)
    rid = added["id"]
    stem = rid.split(":", 1)[1]

    _run(["remove", rid, "--central"])
    # Central bundle is gone.
    central = _central_lib()
    assert not central.entry_dir(stem).is_dir()
    # Project bundle is still there.
    assert (Path(lib) / stem / f"{stem}.json").exists()


# ---------------------------------------------------------------------------
# A rate-limited search must be loud, not a quiet "no match"
# ---------------------------------------------------------------------------


def test_search_exits_nonzero_and_says_unavailable_on_rate_limit(monkeypatch):
    import respx
    import httpx

    with respx.mock:
        respx.get("https://api.openalex.org/works").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"})
        )
        result = runner.invoke(app, ["search", "--title", "Some Paper"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["status"] == "unavailable"
    assert data["unavailable_sources"][0]["retry_after"] == 30


def test_add_by_doi_reports_unavailable_instead_of_go_manual(tmp_path, monkeypatch):
    import respx
    import httpx

    lib = _new_lib(tmp_path)
    f = _sample_file(tmp_path)

    with respx.mock:
        respx.get("https://api.crossref.org/works/10.1000/xyz123").mock(
            return_value=httpx.Response(503)
        )
        respx.get("https://api.datacite.org/dois/10.1000/xyz123").mock(
            return_value=httpx.Response(503)
        )
        result = runner.invoke(
            app, ["add", str(f), "--doi", "10.1000/xyz123", "--library", lib]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["status"] == "unavailable"
    # The old bug: this branch used to advise --manual after a transient outage.
    assert "--manual" not in data["hint"]
