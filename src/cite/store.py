"""Pure on-disk storage for a cite library.

Layout (per-entity *bundle* — see SUITE.md §5.1)::

    <root>/
      cite.toml                 # config: {version, created}
      <id>/<id>.json            # the CSL-JSON record (includes _provenance)
      <id>/<id>.<ext>           # the renamed original document file
      <id>/<id>.md              # (optional) extracted full markdown
      <id>/<id>_artifacts/      # (optional) referenced image artifacts
      <id>/<id>_suppNN.<ext>    # (optional) supplementary material
      <id>/<id>_suppNN.md       # (optional) its extracted markdown

A reference is a *self-contained directory*: everything about it — record,
original file, derived markdown, image artifacts — lives under ``<root>/<id>/``.
That makes a single reference atomically movable, syncable, and removable
(``rm -rf <id>/``). The directory name is the record id (the filename stem),
which is already colon-free and filesystem-safe.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cite.models import PROVENANCE_KEY


class Library:
    """Pure storage layer — does NOT compute hashes or generate filenames."""

    def __init__(
        self, root: Path, *, origin: str = "explicit", searched_from: Path | None = None
    ) -> None:
        self.root = root
        # How this root was chosen, and — when it was searched for rather than
        # named — the directory the upward search started from. Purely
        # descriptive; no method here reads them. They exist so a "there is no
        # library" error can explain *why* it landed on this path, which is the
        # difference between a useful error and a confusing one. One line, not a
        # list of every ancestor tried: the start and "or any parent" say the
        # same thing in a fraction of the tokens (SUITE.md §3).
        self.origin = origin
        self.searched_from = searched_from

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

    # -- Supplementary material (see `cite add-supplement`) -------------- #
    #
    # A supplement is an attachment of its parent reference, not a reference of
    # its own: it has no citation metadata and so cannot produce an id. It lives
    # in the parent's bundle under the stem `<id>_suppNN`, which keeps the
    # one-stem-per-file convention (and therefore Docling's `<stem>_artifacts`
    # image links) intact, and means the whole-directory operations — remove,
    # copy_bundle_to, rename_bundle — carry supplements along for free.

    @staticmethod
    def supplement_stem(record_id: str, n: int) -> str:
        """The stem shared by one supplement's files: ``<id>_supp01``."""
        return f"{record_id}_supp{n:02d}"

    def supplement_path(self, record_id: str, n: int, ext: str) -> Path:
        """Path to a stored supplement file: ``<root>/<id>/<id>_suppNN<ext>``."""
        ext = ext if ext.startswith(".") or not ext else f".{ext}"
        return self.entry_dir(record_id) / f"{self.supplement_stem(record_id, n)}{ext}"

    def supplement_text_path(self, record_id: str, n: int) -> Path:
        """Path to a supplement's extracted markdown: ``<id>/<id>_suppNN.md``."""
        return self.entry_dir(record_id) / f"{self.supplement_stem(record_id, n)}.md"

    def supplement_artifacts_dir(self, record_id: str, n: int) -> Path:
        """A supplement's image artifacts: ``<id>/<id>_suppNN_artifacts/``."""
        return self.entry_dir(record_id) / f"{self.supplement_stem(record_id, n)}_artifacts"

    def clear_supplement_text(self, record_id: str, n: int) -> None:
        """Remove one supplement's markdown + artifacts (mirrors :meth:`clear_text`)."""
        md = self.supplement_text_path(record_id, n)
        if md.exists():
            md.unlink()
        artifacts = self.supplement_artifacts_dir(record_id, n)
        if artifacts.exists():
            shutil.rmtree(artifacts)

    def read_supplement_text(self, record_id: str, n: int) -> str:
        """Return a supplement's markdown. Raise FileNotFoundError if not extracted."""
        path = self.supplement_text_path(record_id, n)
        if not path.exists():
            raise FileNotFoundError(f"No extracted markdown for supplement {n} of {record_id!r}")
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------ #
    # Initialisation
    # ------------------------------------------------------------------ #

    def is_initialized(self) -> bool:
        """Whether ``cite.toml`` exists — the marker that this is a real library."""
        return (self.root / "cite.toml").exists()

    def init(self) -> None:
        """Create root and cite.toml if missing (idempotent).

        Per-entity bundle directories are created lazily on write_record /
        store_file, so there are no fixed sub-trees to provision here.

        Library *creation* is a deliberate, user-approved act — this should
        only be called from the explicit ``cite init`` command, never from
        commands that merely write into an existing library (see
        ``is_initialized``).
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

    def find_id_by_hash(self, file_hash: str) -> str | None:
        """Return the record id (bundle dir name) whose record matches the hash, else None.

        Complements :meth:`find_by_hash` (which returns the record dict). The cross-
        library sync needs the *id* — the on-disk bundle name — to locate and replace
        a bundle when a metadata edit may have changed its content-derived id.
        """
        if not self.root.is_dir():
            return None
        for entry in sorted(self.root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            record_file = entry / f"{entry.name}.json"
            if not record_file.exists():
                continue
            record = json.loads(record_file.read_text(encoding="utf-8"))
            provenance = record.get(PROVENANCE_KEY)
            if isinstance(provenance, dict) and provenance.get("file_hash") == file_hash:
                return entry.name
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

    def copy_bundle_to(self, record_id: str, target: "Library") -> Path:
        """Copy one reference's whole bundle into ``target``, returning the dest path.

        A reference is a self-contained directory, so a recursive directory copy is a
        complete transfer (record + document + markdown + artifacts) with no relational
        integrity to maintain — the basis of cross-library reuse (central library sync
        and ``pull``). Any existing target bundle is removed first, so this is a clean
        replace, not a merge. Raises ``FileNotFoundError`` if the source bundle is
        missing.
        """
        src = self.entry_dir(record_id)
        if not src.is_dir():
            raise FileNotFoundError(f"Record not found: {record_id!r}")
        dst = target.entry_dir(record_id)
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        return dst

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
        consistent. Because the id is the *prefix* of every name in the bundle,
        this moves the directory *and* re-stems every child — ``<old>.json``, the
        stored document, ``<old>.md``, ``<old>_artifacts/``, and each supplement's
        ``<old>_suppNN.*`` — then rewrites every markdown's relative image links
        (``<old>_artifacts`` → ``<new>_artifacts``) so they resolve in place.

        The rename is a prefix substitution rather than a match on ``<old>.``
        precisely so supplements travel with their parent; a narrower rule would
        leave them behind under a stem that no longer names anything.

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
            if not name.startswith(old_id):
                continue
            rest = name[len(old_id):]  # ".pdf", "_artifacts", "_supp01.pdf", …
            child.rename(new_dir / f"{new_id}{rest}")
            # The document is the one child named exactly `<id>.<ext>` and is
            # neither the record nor the markdown. Supplements share the id as a
            # prefix but not as a whole stem, so `_supp01.pdf` is excluded here.
            if rest.startswith(".") and rest not in (".json", ".md"):
                new_doc_filename = f"{new_id}{rest}"

        # Rewrite relative artifact links in *every* markdown (the reference's own
        # and each supplement's), since each points at its own `_artifacts` dir.
        # Anchoring on the `…_artifacts` tail keeps the substitution to link
        # targets — the old id appearing in prose is left alone.
        link = re.compile(re.escape(old_id) + r"((?:_supp\d+)?_artifacts)")
        for md in sorted(new_dir.glob("*.md")):
            text = md.read_text(encoding="utf-8")
            rewritten = link.sub(lambda m: f"{new_id}{m.group(1)}", text)
            if rewritten != text:
                md.write_text(rewritten, encoding="utf-8")

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

