"""Public markdown-extraction facade for the cite package.

Mirrors the ``search`` package shape: this module is the stable entry point;
backend engines live alongside (``docling.py`` today, room for "or similar"
later). Extraction is **deterministic, judgement-free** work — a clean tool
primitive (SUITE.md §1/§6.1) — and the heavy ML engine stays behind the optional
``[extract]`` extra (SUITE.md §7), so importing this package never pulls docling.
"""

from __future__ import annotations

from pathlib import Path

from cite.extract.docling import ExtractorUnavailable, extract_to_markdown

__all__ = ["ExtractorUnavailable", "extract_to_markdown"]


def extract(src_file: Path, md_path: Path, *, vlm_model: str = "granite_docling") -> dict:
    """Extract ``src_file`` to markdown at ``md_path`` (currently via docling)."""
    return extract_to_markdown(src_file, md_path, vlm_model=vlm_model)
