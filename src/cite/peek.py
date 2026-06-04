"""Deterministic first-pass identifier extraction from a document."""

from __future__ import annotations

import re
from pathlib import Path

_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)
_TRAILING = str.maketrans("", "", ".,);")


def _clean_doi(raw: str) -> str:
    """Strip common trailing punctuation from a DOI match."""
    return raw.rstrip(".,);")


def peek(path: Path, max_pages: int = 5) -> dict:
    """Extract identifying info deterministically from a PDF (or generic file).

    Returns:
      {
        "filename": <basename>,
        "embedded_metadata": {...},   # PDF /Info + XMP title/author if available
        "doi": <str|None>,            # first DOI found via regex in first max_pages
        "title_guess": <str|None>,    # from embedded metadata title if present
        "author_guess": <str|None>,   # from embedded metadata author if present
        "suggested_next": <str>,      # see below
      }
    """
    result: dict = {
        "filename": path.name,
        "embedded_metadata": {},
        "doi": None,
        "title_guess": None,
        "author_guess": None,
        "suggested_next": "",
    }

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))

        # --- embedded metadata ---
        meta = reader.metadata
        if meta:
            cleaned: dict = {}
            for key, value in meta.items():
                if value is not None:
                    # Strip the leading '/' from PDF metadata keys
                    clean_key = key.lstrip("/")
                    cleaned[clean_key] = value
            result["embedded_metadata"] = cleaned

            # title / author guesses
            title_val = meta.get("/Title") or meta.get("Title")
            if title_val:
                result["title_guess"] = str(title_val)

            author_val = meta.get("/Author") or meta.get("Author")
            if author_val:
                result["author_guess"] = str(author_val)

        # --- DOI search in first max_pages pages ---
        pages = reader.pages[:max_pages]
        for page in pages:
            text = page.extract_text() or ""
            match = _DOI_RE.search(text)
            if match:
                result["doi"] = _clean_doi(match.group(0))
                break

    except Exception:
        # Not a PDF, unreadable, or any other error — return with empty metadata
        pass

    # --- suggested_next ---
    doi = result["doi"]
    title_guess = result["title_guess"]
    if doi:
        result["suggested_next"] = f"cite search --doi {doi}"
    elif title_guess:
        result["suggested_next"] = f'cite search --title "{title_guess}"'
    else:
        result["suggested_next"] = (
            "read more of the file to find a title/author, then cite search"
        )

    return result
