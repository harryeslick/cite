"""Tests for cite.store.Library (per-entity bundle layout)."""

import json
from pathlib import Path

import pytest

from cite import ops
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


class TestFindIdByHash:
    def test_returns_id_for_matching_hash(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec001", _make_record(file_hash="deadbeef"))
        assert lib.find_id_by_hash("deadbeef") == "rec001"

    def test_returns_none_for_unknown_hash(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        lib.write_record("rec001", _make_record(file_hash="deadbeef"))
        assert lib.find_id_by_hash("zzzzzz") is None

    def test_returns_none_when_library_empty(self, tmp_path: Path):
        lib = Library(tmp_path / "lib")
        lib.init()
        assert lib.find_id_by_hash("anything") is None


class TestCopyBundleTo:
    def _seed(self, lib: Library, rid: str, file_hash: str = "h1") -> None:
        lib.write_record(rid, _make_record(file_hash=file_hash, new_filename=f"{rid}.pdf"))
        doc = lib.entry_dir(rid) / f"{rid}.pdf"
        doc.write_text("document bytes")
        lib.text_path(rid).write_text("# extracted")
        lib.artifacts_dir(rid).mkdir()
        (lib.artifacts_dir(rid) / "img.png").write_bytes(b"x")

    def test_copies_full_bundle(self, tmp_path: Path):
        src_lib = Library(tmp_path / "src")
        src_lib.init()
        dst_lib = Library(tmp_path / "dst")
        dst_lib.init()
        self._seed(src_lib, "rec001")

        dest = src_lib.copy_bundle_to("rec001", dst_lib)
        assert dest == dst_lib.entry_dir("rec001")
        assert dst_lib.read_record("rec001")["title"] == "Test Record"
        assert (dst_lib.entry_dir("rec001") / "rec001.pdf").read_text() == "document bytes"
        assert dst_lib.text_path("rec001").read_text() == "# extracted"
        assert (dst_lib.artifacts_dir("rec001") / "img.png").read_bytes() == b"x"

    def test_overwrites_existing_target(self, tmp_path: Path):
        src_lib = Library(tmp_path / "src")
        src_lib.init()
        dst_lib = Library(tmp_path / "dst")
        dst_lib.init()
        self._seed(src_lib, "rec001", file_hash="newhash")
        # Pre-existing stale bundle at the same id in the target.
        dst_lib.write_record("rec001", _make_record(file_hash="stale"))
        (dst_lib.entry_dir("rec001") / "stray.txt").write_text("leftover")

        src_lib.copy_bundle_to("rec001", dst_lib)
        assert dst_lib.read_record("rec001")[PROVENANCE_KEY]["file_hash"] == "newhash"
        # Clean replace, not a merge: the stray file is gone.
        assert not (dst_lib.entry_dir("rec001") / "stray.txt").exists()

    def test_missing_source_raises(self, tmp_path: Path):
        src_lib = Library(tmp_path / "src")
        src_lib.init()
        dst_lib = Library(tmp_path / "dst")
        dst_lib.init()
        with pytest.raises(FileNotFoundError):
            src_lib.copy_bundle_to("nonexistent", dst_lib)


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


class TestLibraryResolution:
    """How `cite` decides which library a command acts on.

    These exist because the original resolution — a bare relative
    ``Path("library")`` — was silently wrong from any subdirectory, and nothing
    tested it. A read against the wrong root returned an empty success, so the
    bug was invisible.
    """

    def test_walks_up_to_find_the_marker(self, tmp_path: Path, monkeypatch):
        """A `library/` beside the project root is found from deep inside it."""
        project = tmp_path / "project"
        Library(project / "library").init()
        nested = project / "wiki" / "notes"
        nested.mkdir(parents=True)

        monkeypatch.chdir(nested)
        lib = ops.resolve_library(None)

        assert lib.root == project / "library"
        assert lib.origin == "found cite.toml"

    def test_a_library_is_found_from_inside_itself(self, tmp_path: Path, monkeypatch):
        """`cd library && cite doctor` must not resolve `library/library`."""
        root = tmp_path / "project" / "library"
        Library(root).init()

        monkeypatch.chdir(root)
        assert ops.resolve_library(None).root == root

    def test_explicit_library_is_never_searched_from(self, tmp_path: Path, monkeypatch):
        """A named path is taken literally, even when a real library sits above."""
        project = tmp_path / "project"
        Library(project / "library").init()
        monkeypatch.chdir(project)

        lib = ops.resolve_library(Path("elsewhere"))
        assert lib.root == Path("elsewhere")
        assert lib.origin == "--library"

    def test_env_var_is_taken_literally(self, tmp_path: Path, monkeypatch):
        project = tmp_path / "project"
        Library(project / "library").init()
        monkeypatch.chdir(project)
        monkeypatch.setenv("CITE_LIBRARY", str(tmp_path / "env-lib"))

        lib = ops.resolve_library(None)
        assert lib.root == tmp_path / "env-lib"
        assert lib.origin == "$CITE_LIBRARY"

    def test_no_marker_anywhere_reports_where_it_looked(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("CITE_LIBRARY", raising=False)
        monkeypatch.chdir(tmp_path)

        err = ops.require_initialized(ops.resolve_library(None))
        assert err is not None
        assert err["status"] == "no_library"
        # One line, not a list of every ancestor tried.
        assert isinstance(err["searched"], str)
        assert str(tmp_path) in err["searched"]


class TestRenameBundle:
    """`cite update` re-stems a whole bundle when an id-bearing field changes.

    Every file in a bundle carries the id as its stem, so the rename must reach
    all of them — including supplements (``<id>_suppNN.*``), which are not named
    ``<id>.<ext>`` and so are easy to miss.
    """

    def _bundle(self, tmp_path: Path, old: str) -> Library:
        lib = Library(tmp_path / "lib")
        lib.init()
        entry = lib.entry_dir(old)
        entry.mkdir(parents=True)
        (entry / f"{old}.json").write_text(json.dumps(_make_record()), encoding="utf-8")
        (entry / f"{old}.pdf").write_bytes(b"%PDF-paper")
        (entry / f"{old}.md").write_text(f"![f](./{old}_artifacts/image_0.png)", encoding="utf-8")
        (entry / f"{old}_artifacts").mkdir()
        (entry / f"{old}_supp01.pdf").write_bytes(b"%PDF-supp")
        (entry / f"{old}_supp01.md").write_text(
            f"![f](./{old}_supp01_artifacts/image_0.png)", encoding="utf-8"
        )
        (entry / f"{old}_supp01_artifacts").mkdir()
        return lib

    def test_rename_restems_supplements_too(self, tmp_path: Path):
        old, new = "2020-smith-old_aaaaaa", "2021-smith-new_aaaaaa"
        lib = self._bundle(tmp_path, old)

        new_doc = lib.rename_bundle(old, new)

        entry = lib.entry_dir(new)
        assert not lib.entry_dir(old).exists()
        assert sorted(p.name for p in entry.iterdir()) == sorted([
            f"{new}.json", f"{new}.pdf", f"{new}.md", f"{new}_artifacts",
            f"{new}_supp01.pdf", f"{new}_supp01.md", f"{new}_supp01_artifacts",
        ])
        # The supplement PDF must not be mistaken for the reference's document.
        assert new_doc == f"{new}.pdf"

    def test_rename_rewrites_links_in_every_markdown(self, tmp_path: Path):
        old, new = "2020-smith-old_aaaaaa", "2021-smith-new_aaaaaa"
        lib = self._bundle(tmp_path, old)

        lib.rename_bundle(old, new)

        entry = lib.entry_dir(new)
        assert (entry / f"{new}.md").read_text() == f"![f](./{new}_artifacts/image_0.png)"
        assert (entry / f"{new}_supp01.md").read_text() == (
            f"![f](./{new}_supp01_artifacts/image_0.png)"
        )
