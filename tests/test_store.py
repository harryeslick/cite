"""Tests for cite.store.Library (per-entity bundle layout)."""

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
    def test_init_creates_root_and_toml(self, tmp_path: Path):
        lib = Library(tmp_path / "mylib")
        lib.init()
        assert lib.root.is_dir()
        assert (lib.root / "cite.toml").exists()
        # No fixed sub-trees — bundle dirs are created lazily on write.
        assert not (lib.root / "refs").exists()
        assert not (lib.root / "files").exists()

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
        lib.write_record("rec001", _make_record())
        loaded = lib.read_record("rec001")
        assert loaded["title"] == "Test Record"
        assert loaded[PROVENANCE_KEY]["file_hash"] == "abc123"

    def test_write_creates_bundle_dir_and_returns_path(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        path = lib.write_record("rec001", _make_record())
        assert path == lib.root / "rec001" / "rec001.json"
        assert path.exists()
        assert lib.entry_dir("rec001").is_dir()

    def test_read_missing_raises(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        with pytest.raises(FileNotFoundError):
            lib.read_record("nonexistent")

    def test_json_is_pretty(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec001", _make_record())
        text = lib.record_path("rec001").read_text()
        assert "\n" in text


class TestFindByHash:
    def test_returns_record_for_matching_hash(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec001", _make_record(file_hash="deadbeef"))
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
    def test_copies_file_into_bundle(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        src = tmp_path / "source.txt"
        src.write_text("hello world")
        dest = lib.store_file(src, "rec001", "rec001.txt")
        assert dest == lib.entry_dir("rec001") / "rec001.txt"
        assert dest.read_text() == "hello world"

    def test_source_remains_after_copy(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        src = tmp_path / "source.txt"
        src.write_text("contents")
        lib.store_file(src, "rec001", "rec001.txt")
        assert src.exists()

    def test_overwrite_existing_dest_is_fine(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        src = tmp_path / "v2.txt"
        src.write_text("v2 contents")
        lib.store_file(tmp_path / "v2.txt", "rec001", "doc.txt")  # first copy
        lib.store_file(src, "rec001", "doc.txt")  # overwrite
        assert (lib.entry_dir("rec001") / "doc.txt").read_text() == "v2 contents"


class TestListRecords:
    def test_returns_all_records_sorted(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec002", _make_record(file_hash="h2", new_filename="b.pdf"))
        lib.write_record("rec001", _make_record(file_hash="h1", new_filename="a.pdf"))
        records = lib.list_records()
        assert len(records) == 2
        assert records[0][PROVENANCE_KEY]["file_hash"] == "h1"
        assert records[1][PROVENANCE_KEY]["file_hash"] == "h2"

    def test_empty_library_returns_empty_list(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        assert lib.list_records() == []


class TestRemove:
    def test_remove_deletes_whole_bundle(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec001", _make_record(new_filename="rec001.pdf"))
        src = tmp_path / "paper.pdf"
        src.write_text("dummy")
        lib.store_file(src, "rec001", "rec001.pdf")
        lib.text_path("rec001").write_text("# extracted")  # also a derived artifact

        lib.remove("rec001")
        assert not lib.entry_dir("rec001").exists()  # record + file + markdown gone

    def test_remove_missing_record_raises(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        with pytest.raises(FileNotFoundError):
            lib.remove("nonexistent")


class TestExtractedText:
    def test_clear_text_removes_markdown_and_artifacts_only(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec001", _make_record())
        lib.text_path("rec001").write_text("# md")
        lib.artifacts_dir("rec001").mkdir()
        (lib.artifacts_dir("rec001") / "img.png").write_bytes(b"x")

        lib.clear_text("rec001")
        assert not lib.text_path("rec001").exists()
        assert not lib.artifacts_dir("rec001").exists()
        # the record itself survives
        assert lib.record_path("rec001").exists()

    def test_read_text_missing_raises(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec001", _make_record())
        with pytest.raises(FileNotFoundError):
            lib.read_text("rec001")
