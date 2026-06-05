"""Deterministic file hashing + filename generation for the cite library."""

from __future__ import annotations

import hashlib
from pathlib import Path

from slugify import slugify

from cite.models import issued_year

_CHUNK = 65536  # 64 KB

# Suite-wide identity namespace (see SUITE.md §2). A record's *local* id is the
# filename stem (`<year>-<a1>-<a2>-<title>_<hash6>`); its *logical* id — the one
# emitted in JSON and used as a cross-tool foreign key — is that stem prefixed
# with this namespace, e.g. `cite:2023-smith-...`. The prefix is stripped before
# the id ever touches the filesystem, so stored filenames stay colon-free.
ID_NAMESPACE = "cite"
_ID_PREFIX = f"{ID_NAMESPACE}:"


def namespaced_id(local_id: str) -> str:
    """Prefix a bare local id (filename stem) with the suite namespace.

    Idempotent: an already-namespaced id is returned unchanged.
    """
    if local_id.startswith(_ID_PREFIX):
        return local_id
    return f"{_ID_PREFIX}{local_id}"


def local_id(any_id: str) -> str:
    """Strip the suite namespace prefix, yielding the on-disk filename stem.

    Accepts either form, so callers can pass `cite:2023-x` or the bare `2023-x`.
    """
    if any_id.startswith(_ID_PREFIX):
        return any_id[len(_ID_PREFIX):]
    return any_id


def content_hash(path: Path) -> str:
    """Full SHA-256 hex of the file's bytes (streamed in 64 KB chunks)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def short_hash(full_hash: str, n: int = 6) -> str:
    """First n hex chars of a full hash."""
    return full_hash[:n]


def author_components(record: dict) -> tuple[str, str]:
    """Return (author1_slug, author2_slot) for the filename.

    The second slot modifies the first author and is **empty** when there is
    nothing to put there; `build_filename` drops empty slots so no literal
    placeholder ever appears in a filename. Rules, based on the record's
    `author` list (fallback to `editor` if no author):
      - 0 authors  -> ("", "")             # no author segment at all
      - 1 author   -> (slug(family/literal), "")
      - 2 authors  -> (slug(fam1), slug(fam2))
      - 3+ authors -> (slug(fam1), "etal")
    """
    authors: list[dict] = record.get("author") or record.get("editor") or []

    def _name(a: dict) -> str:
        raw = a.get("family") or a.get("literal") or ""
        result = slugify(raw)
        return result if result else "anon"

    n = len(authors)
    if n == 0:
        return ("", "")
    if n == 1:
        return (_name(authors[0]), "")
    if n == 2:
        return (_name(authors[0]), _name(authors[1]))
    # 3+
    return (_name(authors[0]), "etal")


def build_filename(record: dict, full_hash: str, ext: str) -> str:
    """Compose `<year>-<author1>[-<author2|etal>]-<title>_<shorthash>.<ext>`.

    - year   = issued_year(record) or 'nd'
    - author slots = author_components(record); empty slots are omitted, so a
      single-author file is `<year>-<author1>-<title>...` (no placeholder) and
      an author-less file is `<year>-<title>...`.
    - title  = slugified title, word-boundary truncated to <= 40 chars
    - ext    = original extension WITHOUT leading dot; if empty, omit the dot
    - shorthash = short_hash(full_hash)
    """
    year = str(issued_year(record) or "nd")
    a1, a2 = author_components(record)
    raw_title = record.get("title") or ""
    title_slug = slugify(raw_title, max_length=40, word_boundary=True)
    if not title_slug:
        title_slug = "untitled"
    sh = short_hash(full_hash)
    parts = [p for p in (year, a1, a2, title_slug) if p]
    stem = f"{'-'.join(parts)}_{sh}"
    if ext:
        return f"{stem}.{ext}"
    return stem


def record_id(record: dict, full_hash: str) -> str:
    """The library id = build_filename without the extension (the stem)."""
    # We call build_filename with no ext to get the bare stem
    return build_filename(record, full_hash, "")
