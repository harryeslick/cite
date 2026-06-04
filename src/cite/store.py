"""Pure on-disk storage for a cite library.

Layout::

    <root>/
      cite.toml          # config: {version, created}
      refs/<id>.json     # one CSL-JSON record per reference (includes _provenance)
      files/<id>.<ext>   # the renamed document files
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
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def refs_dir(self) -> Path:
        return self.root / "refs"

    @property
    def files_dir(self) -> Path:
        return self.root / "files"

    # ------------------------------------------------------------------ #
    # Initialisation
    # ------------------------------------------------------------------ #

    def init(self) -> None:
        """Create root, refs/, files/, and cite.toml if missing (idempotent)."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.refs_dir.mkdir(exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)

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
        """Write refs/<record_id>.json (pretty, sorted keys=False, ensure_ascii=False).
        Return the path.
        """
        dest = self.refs_dir / f"{record_id}.json"
        dest.write_text(
            json.dumps(record, indent=2, sort_keys=False, ensure_ascii=False),
            encoding="utf-8",
        )
        return dest

    def read_record(self, record_id: str) -> dict:
        """Load refs/<record_id>.json. Raise FileNotFoundError if missing."""
        path = self.refs_dir / f"{record_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Record not found: {record_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_records(self) -> list[dict]:
        """Return all records sorted by id."""
        paths = sorted(self.refs_dir.glob("*.json"), key=lambda p: p.stem)
        records = []
        for path in paths:
            records.append(json.loads(path.read_text(encoding="utf-8")))
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

    def store_file(self, src: Path, new_filename: str) -> Path:
        """Copy src into files/<new_filename> (shutil.copy2). Return the dest path.
        Do not delete the source. If dest exists, overwrite is fine.
        """
        dest = self.files_dir / new_filename
        shutil.copy2(src, dest)
        return dest

    def remove(self, record_id: str, delete_file: bool = False) -> None:
        """Delete refs/<record_id>.json; if delete_file, also delete the matching
        files/<id>.* (look up new_filename from the record's _provenance first).
        """
        ref_path = self.refs_dir / f"{record_id}.json"
        if not ref_path.exists():
            raise FileNotFoundError(f"Record not found: {record_id!r}")

        if delete_file:
            record = json.loads(ref_path.read_text(encoding="utf-8"))
            provenance = record.get(PROVENANCE_KEY)
            if isinstance(provenance, dict):
                new_filename = provenance.get("new_filename")
                if new_filename:
                    file_path = self.files_dir / new_filename
                    if file_path.exists():
                        file_path.unlink()

        ref_path.unlink()

    # ------------------------------------------------------------------ #
    # Config
    # ------------------------------------------------------------------ #

    def _read_config(self) -> dict:
        toml_path = self.root / "cite.toml"
        if not toml_path.exists():
            return {}
        return tomllib.loads(toml_path.read_text(encoding="utf-8"))
