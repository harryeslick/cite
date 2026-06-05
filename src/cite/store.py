"""Pure on-disk storage for a cite library.

Layout (per-entity *bundle* — see SUITE.md §5.1)::

    <root>/
      cite.toml                 # config: {version, created}
      <id>/<id>.json            # the CSL-JSON record (includes _provenance)
      <id>/<id>.<ext>           # the renamed original document file
      <id>/<id>.md              # (optional) extracted full markdown
      <id>/<id>_artifacts/      # (optional) referenced image artifacts

A reference is a *self-contained directory*: everything about it — record,
original file, derived markdown, image artifacts — lives under ``<root>/<id>/``.
That makes a single reference atomically movable, syncable, and removable
(``rm -rf <id>/``). The directory name is the record id (the filename stem),
which is already colon-free and filesystem-safe.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cite.models import PROVENANCE_KEY


class Library:
    """Pure storage layer — does NOT compute hashes or generate filenames."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # ------------------------------------------------------------------ #
    # Per-entity bundle paths
    # ------------------------------------------------------------------ #

    def entry_dir(self, record_id: str) -> Path:
        """The bundle directory for one reference: ``<root>/<id>/``."""
        return self.root / record_id

    def record_path(self, record_id: str) -> Path:
        """Path to the CSL-JSON record: ``<root>/<id>/<id>.json``."""
        return self.entry_dir(record_id) / f"{record_id}.json"

    def text_path(self, record_id: str) -> Path:
        """Path to the extracted markdown: ``<root>/<id>/<id>.md``."""
        return self.entry_dir(record_id) / f"{record_id}.md"

    def artifacts_dir(self, record_id: str) -> Path:
        """Directory of referenced image artifacts: ``<root>/<id>/<id>_artifacts/``.

        The name mirrors what Docling's ``save_as_markdown(..., REFERENCED)`` emits
        beside a markdown file (``<md-stem>_artifacts``), so its relative image
        links resolve in place.
        """
        return self.entry_dir(record_id) / f"{record_id}_artifacts"

    # ------------------------------------------------------------------ #
    # Initialisation
    # ------------------------------------------------------------------ #

    def init(self) -> None:
        """Create root and cite.toml if missing (idempotent).

        Per-entity bundle directories are created lazily on write_record /
        store_file, so there are no fixed sub-trees to provision here.
        """
        self.root.mkdir(parents=True, exist_ok=True)

        toml_path = self.root / "cite.toml"
        if not toml_path.exists():
            created = datetime.now(timezone.utc).isoformat(timespec="seconds")
            toml_path.write_text(
                f'version = "1"\ncreated = "{created}"\n',
                encoding="utf-8",
            )

    # ------------------------------------------------------------------ #
    # Record I/O
    # ------------------------------------------------------------------ #

    def write_record(self, record_id: str, record: dict) -> Path:
        """Write ``<root>/<id>/<id>.json`` (creating the bundle dir). Return the path."""
        dest = self.record_path(record_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(record, indent=2, sort_keys=False, ensure_ascii=False),
            encoding="utf-8",
        )
        return dest

    def read_record(self, record_id: str) -> dict:
        """Load ``<root>/<id>/<id>.json``. Raise FileNotFoundError if missing."""
        path = self.record_path(record_id)
        if not path.exists():
            raise FileNotFoundError(f"Record not found: {record_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_records(self) -> list[dict]:
        """Return all records sorted by id.

        Walks each bundle directory and loads ``<id>/<id>.json`` where present;
        anything else under the root (e.g. ``cite.toml``, stray dirs) is ignored.
        """
        if not self.root.is_dir():
            return []
        records = []
        for entry in sorted(self.root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            record_file = entry / f"{entry.name}.json"
            if record_file.exists():
                records.append(json.loads(record_file.read_text(encoding="utf-8")))
        return records

    # ------------------------------------------------------------------ #
    # Deduplication
    # ------------------------------------------------------------------ #

    def find_by_hash(self, file_hash: str) -> dict | None:
        """Return an existing record whose _provenance.file_hash matches, else None."""
        for record in self.list_records():
            provenance = record.get(PROVENANCE_KEY)
            if isinstance(provenance, dict) and provenance.get("file_hash") == file_hash:
                return record
        return None

    # ------------------------------------------------------------------ #
    # File management
    # ------------------------------------------------------------------ #

    def store_file(self, src: Path, record_id: str, new_filename: str) -> Path:
        """Copy src into the reference's bundle as ``<root>/<id>/<new_filename>``.

        Creates the bundle dir if needed (shutil.copy2). Does not delete the
        source. Overwriting an existing dest is fine.
        """
        entry = self.entry_dir(record_id)
        entry.mkdir(parents=True, exist_ok=True)
        dest = entry / new_filename
        shutil.copy2(src, dest)
        return dest

    def remove(self, record_id: str) -> None:
        """Delete the whole bundle directory (record + file + markdown + artifacts).

        A reference is its directory, so removal is all-or-nothing and atomic.
        Raise FileNotFoundError if the record does not exist.
        """
        if not self.record_path(record_id).exists():
            raise FileNotFoundError(f"Record not found: {record_id!r}")
        shutil.rmtree(self.entry_dir(record_id))

    # ------------------------------------------------------------------ #
    # Extracted markdown (see `cite extract`)
    # ------------------------------------------------------------------ #

    def clear_text(self, record_id: str) -> None:
        """Remove a reference's extracted markdown + artifacts (not the whole bundle).

        Lets ``cite extract`` re-run idempotently: wipe prior output, then write
        fresh. Leaves the record and the original file intact.
        """
        md = self.text_path(record_id)
        if md.exists():
            md.unlink()
        artifacts = self.artifacts_dir(record_id)
        if artifacts.exists():
            shutil.rmtree(artifacts)

    def read_text(self, record_id: str) -> str:
        """Return the extracted markdown. Raise FileNotFoundError if not extracted."""
        path = self.text_path(record_id)
        if not path.exists():
            raise FileNotFoundError(f"No extracted markdown for: {record_id!r}")
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------ #
    # Config
    # ------------------------------------------------------------------ #

    def _read_config(self) -> dict:
        toml_path = self.root / "cite.toml"
        if not toml_path.exists():
            return {}
        return tomllib.loads(toml_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ #
    # Legacy layout migration (refs/ + files/  ->  per-entity bundle)
    # ------------------------------------------------------------------ #

    def migrate_layout(self) -> dict:
        """Migrate a legacy ``refs/`` + ``files/`` library to per-entity bundles.

        Idempotent: a no-op on an already-bundled library. For each
        ``refs/<id>.json`` the record's ``_provenance.new_filename`` names the
        original file in ``files/``; both are moved into ``<root>/<id>/`` and the
        now-empty ``refs/`` and ``files/`` are removed.

        Returns a summary dict ``{migrated, skipped_no_file}``.
        """
        refs_dir = self.root / "refs"
        files_dir = self.root / "files"
        if not refs_dir.is_dir():
            return {"migrated": 0, "skipped_no_file": 0, "note": "already bundled"}

        migrated = 0
        skipped_no_file = 0
        for ref_path in sorted(refs_dir.glob("*.json")):
            record_id = ref_path.stem
            record = json.loads(ref_path.read_text(encoding="utf-8"))
            entry = self.entry_dir(record_id)
            entry.mkdir(parents=True, exist_ok=True)

            # Move the record.
            ref_path.replace(self.record_path(record_id))

            # Move the original file, if locatable.
            new_filename = record.get(PROVENANCE_KEY, {}).get("new_filename")
            if new_filename:
                old_file = files_dir / new_filename
                if old_file.exists():
                    old_file.replace(entry / new_filename)
                else:
                    skipped_no_file += 1
            migrated += 1

        # Drop the now-empty legacy trees (rmtree tolerates leftovers).
        for legacy in (refs_dir, files_dir):
            if legacy.is_dir() and not any(legacy.iterdir()):
                legacy.rmdir()

        return {"migrated": migrated, "skipped_no_file": skipped_no_file}
