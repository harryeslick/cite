"""Decide whether a document already carries a usable text layer.

A born-digital PDF hands over its text losslessly; a scanned one has no text at
all and must be read by a vision model. Those two cases want completely
different Docling pipelines — and the difference is minutes versus tens of
minutes, plus the risk that a VLM paraphrases text that was already perfect.

This module answers the one question that separates them, deterministically and
in well under a second, using ``pypdf`` (already a core dependency — the same
reader :mod:`cite.peek` uses). No ML, no network, no docling import.
"""

from __future__ import annotations

import statistics
from pathlib import Path

# A page of body text runs to a few thousand characters. The threshold only has
# to separate "real text layer" from "stray page numbers and header artefacts an
# otherwise-scanned PDF may still carry", so it sits deliberately low.
_MIN_MEDIAN_CHARS = 200

# Below this a page counts as having no text at all (a plate, or a scan).
_EMPTY_PAGE_CHARS = 50

# Enough pages to be representative of a book without reading all of one.
_MAX_SAMPLE = 20

TEXT = "text"
SCANNED = "scanned"


def _sample_indices(n_pages: int, limit: int = _MAX_SAMPLE) -> list[int]:
    """Evenly-spaced page indices across the whole document.

    Even spacing rather than the first N pages: front matter (title page,
    copyright, blank verso) is the least text-dense part of a book and would
    badly under-represent a document that is text-rich from chapter one on.
    """
    if n_pages <= limit:
        return list(range(n_pages))
    step = n_pages / limit
    return [int(i * step) for i in range(limit)]


def probe_text_layer(path: Path) -> dict:
    """Report how much extractable text ``path`` already contains.

    Returns::

        {"pages": int,                  # total pages (0 if unreadable)
         "sampled": int,                # pages actually inspected
         "median_chars_per_page": int,  # across the sample
         "pages_without_text": int,     # within the sample
         "verdict": "text" | "scanned"}

    A ``scanned`` verdict is the safe answer, so anything we cannot read — a
    non-PDF, an encrypted or corrupt file, a missing pypdf — returns it. The
    caller then reaches for the VLM, which handles those cases; guessing ``text``
    on an unreadable file would instead produce empty markdown.
    """
    result = {
        "pages": 0,
        "sampled": 0,
        "median_chars_per_page": 0,
        "pages_without_text": 0,
        "verdict": SCANNED,
    }

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        n_pages = len(reader.pages)
        if n_pages == 0:
            return result

        indices = _sample_indices(n_pages)
        lengths = [len(reader.pages[i].extract_text() or "") for i in indices]
    except Exception:
        # Not a PDF, unreadable, encrypted, or pypdf absent — see the docstring:
        # the VLM is the fallback that can still handle it.
        return result

    median = int(statistics.median(lengths))
    empty = sum(1 for n in lengths if n < _EMPTY_PAGE_CHARS)

    result.update(
        pages=n_pages,
        sampled=len(lengths),
        median_chars_per_page=median,
        pages_without_text=empty,
    )
    # Both conditions matter: the median rules out a document that is mostly
    # blank, and the majority check rules out one where a handful of text-heavy
    # pages (an OCR'd cover sheet stapled to a scan) drag the median up.
    if median >= _MIN_MEDIAN_CHARS and empty <= len(lengths) / 2:
        result["verdict"] = TEXT
    return result
