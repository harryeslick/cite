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

    def rename_bundle(self, old_id: str, new_id: str) -> str:
        """Re-stem an entire reference bundle from ``old_id`` to ``new_id``.

        Used by ``cite update`` when an edit changes an id-bearing field (year,
        author, or title): the deterministic id is recomputed and no longer
        matches the directory name, so the bundle must be renamed to stay
        consistent. Because the id is also the stem of every file in the bundle,
        this moves the directory *and* re-stems each contained file —
        ``<old>.json`` → ``<new>.json``, the stored document, ``<old>.md``, and
        ``<old>_artifacts/`` — then rewrites the markdown's relative image links
        (``<old>_artifacts`` → ``<new>_artifacts``) so they resolve in place.

        The content hash is unchanged by a metadata edit, so the ``_hash6``
        suffix is stable across the rename and dedup identity is preserved.

        Returns the document's *new* filename (the re-stemmed ``<new>.<ext>``) so
        the caller can refresh ``_provenance.new_filename``; returns ``""`` if the
        bundle held no document file. Raises FileNotFoundError if ``old_id`` is
        absent and FileExistsError if ``new_id`` already exists (a real id clash
        the caller must surface rather than silently clobber).
        """
        old_dir = self.entry_dir(old_id)
        if not old_dir.is_dir():
            raise FileNotFoundError(f"Record not found: {old_id!r}")
        new_dir = self.entry_dir(new_id)
        if new_dir.exists():
            raise FileExistsError(f"Target id already exists: {new_id!r}")

        # Move the directory first, then re-stem its contents in place.
        old_dir.rename(new_dir)

        new_doc_filename = ""
        for child in sorted(new_dir.iterdir()):
            name = child.name
            if name == f"{old_id}_artifacts":
                child.rename(new_dir / f"{new_id}_artifacts")
            elif name.startswith(f"{old_id}."):
                suffix = name[len(old_id):]  # includes the leading dot, e.g. ".pdf"
                child.rename(new_dir / f"{new_id}{suffix}")
                # Anything that isn't the record or the markdown is the document.
                if suffix not in (".json", ".md"):
                    new_doc_filename = f"{new_id}{suffix}"

        # Rewrite the markdown's relative artifact links to the new stem.
        md = self.text_path(new_id)
        if md.exists():
            text = md.read_text(encoding="utf-8").replace(
                f"{old_id}_artifacts", f"{new_id}_artifacts"
            )
            md.write_text(text, encoding="utf-8")

        return new_doc_filename

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
    # Pre-add staging cache (see `cite prepare`)
    #
    # The canonical reference id is derived from the *citation* (year, author,
    # title), which `prepare` exists to help discover — so before `add` there is
    # no id to key an extraction on. The one identifier available both before and
    # after `add` is the file's content hash, so a pre-add extraction is cached
    # under it here. `add` later recomputes that same hash (it needs it for dedup)
    # and `adopt_staged` moves the cached markdown into the final bundle, so the
    # expensive VLM pass runs exactly once.
    # ------------------------------------------------------------------ #

    def staging_dir(self) -> Path:
        """Root of the pre-add extraction cache: ``<root>/.staging/``.

        Dotted name keeps it out of :meth:`list_records` (which only counts
        bundle dirs holding ``<name>/<name>.json``), so staged work is invisible
        to the library proper until adopted.
        """
        return self.root / ".staging"

    def staging_entry(self, file_hash: str) -> Path:
        """Staging bundle for one file's extraction: ``<root>/.staging/<hash>/``."""
        return self.staging_dir() / file_hash

    def staging_text_path(self, file_hash: str) -> Path:
        """Staged markdown path: ``<root>/.staging/<hash>/<hash>.md``."""
        return self.staging_entry(file_hash) / f"{file_hash}.md"

    def staging_artifacts_dir(self, file_hash: str) -> Path:
        """Staged image artifacts: ``<root>/.staging/<hash>/<hash>_artifacts/``."""
        return self.staging_entry(file_hash) / f"{file_hash}_artifacts"

    def staging_meta_path(self, file_hash: str) -> Path:
        """Sidecar holding the extraction provenance for a staged file."""
        return self.staging_entry(file_hash) / "meta.json"

    def has_staged(self, file_hash: str) -> bool:
        """True if a completed staged extraction (markdown present) exists."""
        return self.staging_text_path(file_hash).exists()

    def read_staged_meta(self, file_hash: str) -> dict:
        """Return the staged extraction's provenance sidecar (``{}`` if absent)."""
        path = self.staging_meta_path(file_hash)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def write_staged_meta(self, file_hash: str, meta: dict) -> Path:
        """Write the extraction provenance sidecar for a staged file."""
        dest = self.staging_meta_path(file_hash)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return dest

    def clear_staged(self, file_hash: str) -> None:
        """Remove a file's whole staging entry (markdown + artifacts + meta)."""
        entry = self.staging_entry(file_hash)
        if entry.exists():
            shutil.rmtree(entry)

    def adopt_staged(self, file_hash: str, record_id: str) -> dict | None:
        """Move a staged extraction into reference ``record_id``'s bundle.

        Returns the extraction provenance (with ``markdown_path`` rewritten to the
        bundle-relative ``<id>/<id>.md``) when a staged extraction was adopted, or
        ``None`` when nothing was staged for this hash. The expensive VLM work was
        already paid by `prepare`, so this only renames/moves and rewrites the
        markdown's image links from the hash stem to the id stem.
        """
        staged_md = self.staging_text_path(file_hash)
        if not staged_md.exists():
            return None

        meta = self.read_staged_meta(file_hash)

        # Rewrite the relative image links (Docling references "<stem>_artifacts/…",
        # and the stem changes from the hash to the record id on adoption).
        text = staged_md.read_text(encoding="utf-8").replace(
            f"{file_hash}_artifacts", f"{record_id}_artifacts"
        )
        dest_md = self.text_path(record_id)
        dest_md.parent.mkdir(parents=True, exist_ok=True)
        dest_md.write_text(text, encoding="utf-8")

        staged_art = self.staging_artifacts_dir(file_hash)
        if staged_art.exists():
            dest_art = self.artifacts_dir(record_id)
            if dest_art.exists():
                shutil.rmtree(dest_art)
            shutil.move(str(staged_art), str(dest_art))

        self.clear_staged(file_hash)
        meta["markdown_path"] = f"{record_id}/{record_id}.md"
        return meta

    # ------------------------------------------------------------------ #
    # Config
    # ------------------------------------------------------------------ #

    def _read_config(self) -> dict:
        toml_path = self.root / "cite.toml"
        if not toml_path.exists():
            return {}
        return tomllib.loads(toml_path.read_text(encoding="utf-8"))

