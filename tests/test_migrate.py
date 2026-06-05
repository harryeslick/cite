"""Tests for `cite migrate-layout` (legacy refs/+files/ -> per-entity bundles)."""

import json
from pathlib import Path

from typer.testing import CliRunner

from cite.cli import app
from cite.models import PROVENANCE_KEY

runner = CliRunner()


def _run(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _legacy_library(root: Path) -> tuple[str, str]:
    """Build a legacy refs/+files/ library with one reference; return (id, filename)."""
    rid = "2022-agency-annual-report_abc123"
    filename = f"{rid}.pdf"
    (root / "refs").mkdir(parents=True)
    (root / "files").mkdir(parents=True)
    (root / "cite.toml").write_text('version = "1"\ncreated = "2026-06-04T00:00:00Z"\n')
    record = {
        "type": "report",
        "title": "Annual Report",
        PROVENANCE_KEY: {
            "cite_type": "other-report",
            "original_filename": "report.pdf",
            "new_filename": filename,
            "date_added": "2026-06-04T00:00:00Z",
            "file_hash": "abc123",
            "source": "manual",
        },
    }
    (root / "refs" / f"{rid}.json").write_text(json.dumps(record))
    (root / "files" / filename).write_text("pdf bytes")
    return rid, filename


def test_migrate_moves_record_and_file_into_bundle(tmp_path):
    root = tmp_path / "lib"
    rid, filename = _legacy_library(root)

    out = _run(["migrate-layout", "--library", str(root)])
    assert out["status"] == "ok"
    assert out["migrated"] == 1
    assert out["skipped_no_file"] == 0

    # Bundle created; legacy trees gone.
    assert (root / rid / f"{rid}.json").exists()
    assert (root / rid / filename).read_text() == "pdf bytes"
    assert not (root / "refs").exists()
    assert not (root / "files").exists()

    # The migrated record is readable through the normal CLI.
    got = _run(["get", rid, "--library", str(root)])
    assert got["title"] == "Annual Report"


def test_migrate_is_idempotent(tmp_path):
    root = tmp_path / "lib"
    _legacy_library(root)
    _run(["migrate-layout", "--library", str(root)])  # first pass

    again = _run(["migrate-layout", "--library", str(root)])  # second pass
    assert again["migrated"] == 0
    assert again.get("note") == "already bundled"


def test_migrate_reports_missing_source_file(tmp_path):
    root = tmp_path / "lib"
    rid, filename = _legacy_library(root)
    (root / "files" / filename).unlink()  # original file lost

    out = _run(["migrate-layout", "--library", str(root)])
    assert out["migrated"] == 1
    assert out["skipped_no_file"] == 1
    assert (root / rid / f"{rid}.json").exists()  # record still migrated
