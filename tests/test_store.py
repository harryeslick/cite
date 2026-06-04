"""Tests for cite.store.Library."""

import json
from pathlib import Path

import pytest

from cite.store import Library
from cite.models import PROVENANCE_KEY


def _make_record(file_hash: str = "abc123", new_filename: str = "doc.pdf") -> dict:
    return {
        "type": "article-journal",
        "title": "Test Record",
        PROVENANCE_KEY: {
            "cite_type": "journal-article",
            "original_filename": "original.pdf",
            "new_filename": new_filename,
            "date_added": "2026-06-04T00:00:00+00:00",
            "file_hash": file_hash,
            "source": "manual",
            "source_id": None,
        },
    }


class TestInit:
    def test_init_creates_dirs_and_toml(self, tmp_path: Path):
        lib = Library(tmp_path / "mylib")
        lib.init()
        assert lib.root.is_dir()
        assert lib.refs_dir.is_dir()
        assert lib.files_dir.is_dir()
        assert (lib.root / "cite.toml").exists()

    def test_init_is_idempotent(self, tmp_path: Path):
        lib = Library(tmp_path / "mylib")
        lib.init()
        lib.init()  # should not raise
        assert (lib.root / "cite.toml").exists()

    def test_toml_has_version_and_created(self, tmp_path: Path):
        lib = Library(tmp_path / "mylib")
        lib.init()
        content = (lib.root / "cite.toml").read_text()
        assert "version" in content
        assert "created" in content


class TestWriteAndReadRecord:
    def test_round_trip(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        record = _make_record()
        lib.write_record("rec001", record)
        loaded = lib.read_record("rec001")
        assert loaded["title"] == "Test Record"
        assert loaded[PROVENANCE_KEY]["file_hash"] == "abc123"

    def test_write_returns_path(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        path = lib.write_record("rec001", _make_record())
        assert path == lib.refs_dir / "rec001.json"
        assert path.exists()

    def test_read_missing_raises(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        with pytest.raises(FileNotFoundError):
            lib.read_record("nonexistent")

    def test_json_is_pretty(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec001", _make_record())
        text = (lib.refs_dir / "rec001.json").read_text()
        # Pretty JSON has newlines and indentation
        assert "\n" in text


class TestFindByHash:
    def test_returns_record_for_matching_hash(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        record = _make_record(file_hash="deadbeef")
        lib.write_record("rec001", record)
        found = lib.find_by_hash("deadbeef")
        assert found is not None
        assert found["title"] == "Test Record"

    def test_returns_none_for_unknown_hash(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec001", _make_record(file_hash="deadbeef"))
        assert lib.find_by_hash("zzzzzz") is None

    def test_returns_none_when_library_empty(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        assert lib.find_by_hash("anything") is None


class TestStoreFile:
    def test_copies_file_to_files_dir(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        src = tmp_path / "source.txt"
        src.write_text("hello world")
        dest = lib.store_file(src, "doc-2026.txt")
        assert dest == lib.files_dir / "doc-2026.txt"
        assert dest.read_text() == "hello world"

    def test_source_remains_after_copy(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        src = tmp_path / "source.txt"
        src.write_text("contents")
        lib.store_file(src, "stored.txt")
        assert src.exists()

    def test_overwrite_existing_dest_is_fine(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        src = tmp_path / "v2.txt"
        src.write_text("v2 contents")
        # write something first
        (lib.files_dir / "doc.txt").write_text("old")
        lib.store_file(src, "doc.txt")
        assert (lib.files_dir / "doc.txt").read_text() == "v2 contents"


class TestListRecords:
    def test_returns_all_records_sorted(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec002", _make_record(file_hash="h2", new_filename="b.pdf"))
        lib.write_record("rec001", _make_record(file_hash="h1", new_filename="a.pdf"))
        records = lib.list_records()
        assert len(records) == 2
        # sorted by id: rec001 before rec002
        assert records[0][PROVENANCE_KEY]["file_hash"] == "h1"
        assert records[1][PROVENANCE_KEY]["file_hash"] == "h2"

    def test_empty_library_returns_empty_list(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        assert lib.list_records() == []


class TestRemove:
    def test_remove_deletes_ref(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec001", _make_record())
        lib.remove("rec001")
        assert not (lib.refs_dir / "rec001.json").exists()

    def test_remove_missing_record_raises(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        with pytest.raises(FileNotFoundError):
            lib.remove("nonexistent")

    def test_remove_with_delete_file_removes_stored_file(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        record = _make_record(new_filename="paper.pdf")
        lib.write_record("rec001", record)
        # put a dummy file in files/
        (lib.files_dir / "paper.pdf").write_text("dummy")
        lib.remove("rec001", delete_file=True)
        assert not (lib.refs_dir / "rec001.json").exists()
        assert not (lib.files_dir / "paper.pdf").exists()

    def test_remove_without_delete_file_leaves_file(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        record = _make_record(new_filename="paper.pdf")
        lib.write_record("rec001", record)
        (lib.files_dir / "paper.pdf").write_text("dummy")
        lib.remove("rec001", delete_file=False)
        assert not (lib.refs_dir / "rec001.json").exists()
        assert (lib.files_dir / "paper.pdf").exists()
