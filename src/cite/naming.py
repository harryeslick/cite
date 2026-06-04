"""Deterministic file hashing + filename generation for the cite library."""

from __future__ import annotations

import hashlib
from pathlib import Path

from slugify import slugify

from cite.models import issued_year

_CHUNK = 65536  # 64 KB


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

    Rules based on the record's `author` list (fallback to `editor` if no author):
      - 0 authors  -> ("none", "none")
      - 1 author   -> (slug(family or literal), "none")
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
        return ("none", "none")
    if n == 1:
        return (_name(authors[0]), "none")
    if n == 2:
        return (_name(authors[0]), _name(authors[1]))
    # 3+
    return (_name(authors[0]), "etal")


def build_filename(record: dict, full_hash: str, ext: str) -> str:
    """Compose `<year>-<author1>-<author2|etal|none>-<title>_<shorthash>.<ext>`.

    - year   = issued_year(record) or 'nd'
    - title  = slugified title, word-boundary truncated to <= 40 chars
    - ext    = original extension WITHOUT leading dot; if empty, omit the dot
    - shorthash = short_hash(full_hash)
    """
    year = issued_year(record) or "nd"
    a1, a2 = author_components(record)
    raw_title = record.get("title") or ""
    title_slug = slugify(raw_title, max_length=40, word_boundary=True)
    if not title_slug:
        title_slug = "untitled"
    sh = short_hash(full_hash)
    stem = f"{year}-{a1}-{a2}-{title_slug}_{sh}"
    if ext:
        return f"{stem}.{ext}"
    return stem


def record_id(record: dict, full_hash: str) -> str:
    """The library id = build_filename without the extension (the stem)."""
    # We call build_filename with no ext to get the bare stem
    return build_filename(record, full_hash, "")
