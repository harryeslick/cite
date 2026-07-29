"""Public markdown-extraction facade for the cite package.

Mirrors the ``search`` package shape: this module is the stable entry point;
backend engines live alongside (``docling.py`` today, room for "or similar"
later). Extraction is **deterministic, judgement-free** work — a clean tool
primitive (SUITE.md §1/§6.1) — and the heavy ML engine stays behind the optional
``[extract]`` extra (SUITE.md §7), so importing this package never pulls docling.
"""

from __future__ import annotations

from pathlib import Path

from cite.extract.docling import AUTO, ENGINES, ExtractorUnavailable, extract_to_markdown
from cite.extract.probe import probe_text_layer

__all__ = [
    "AUTO",
    "ENGINES",
    "ExtractorUnavailable",
    "extract_to_markdown",
    "probe_text_layer",
]


def extract(
    src_file: Path,
    md_path: Path,
    *,
    engine: str = AUTO,
    vlm_model: str = "granite_docling",
) -> dict:
    """Extract ``src_file`` to markdown at ``md_path`` (currently via docling)."""
    return extract_to_markdown(src_file, md_path, engine=engine, vlm_model=vlm_model)
